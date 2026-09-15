#!/usr/bin/env python3
import socket, struct, time, sys, cv2, numpy as np, argparse, json, threading, signal, os, re
from picamera2 import Picamera2

# ───────── GPIO Relay/LED Manager ─────────
class RelayLED(threading.Thread):
    """
    상태:
      - 'OFF'   : 소등(기본)
      - 'ON'    : 지속 점등 (hold_s 뒤 자동 OFF)
      - 'BLINK' : 깜빡임 (interval_s)
    핀 지정:
      - gpio_mode: 'board' | 'bcm'
      - gpio_pin : BOARD 기준 수, 또는 BCM 번호 (모드에 따라)
      - active_low: True면 출력 LOW가 켜짐(릴레이 보드 Active-LOW)
    """
    def __init__(self, gpio_mode="board", gpio_pin=16, active_low=False, interval_s=0.4):
        super().__init__(daemon=True)
        self.gpio_mode = gpio_mode.lower()
        self.pin = int(gpio_pin)
        self.active_low = bool(active_low)
        self.interval = float(interval_s)
        self._state = "OFF"
        self._hold_until = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()

        try:
            import RPi.GPIO as GPIO
            self.GPIO = GPIO
            if self.GPIO.getmode() is None:
                self.GPIO.setwarnings(False)
                if self.gpio_mode == "bcm":
                    self.GPIO.setmode(self.GPIO.BCM)
                else:
                    self.GPIO.setmode(self.GPIO.BOARD)
            self.GPIO.setup(self.pin, self.GPIO.OUT, initial=self._lvl_off())
            self._ok = True
        except Exception as e:
            print("[GPIO] init failed:", e)
            self._ok = False

    def _lvl_on(self):
        return self.GPIO.LOW if self.active_low else self.GPIO.HIGH

    def _lvl_off(self):
        return self.GPIO.HIGH if self.active_low else self.GPIO.LOW

    def set_off(self):
        with self._lock:
            self._state = "OFF"
            self._hold_until = 0.0

    def set_blink(self):
        with self._lock:
            self._state = "BLINK"
            self._hold_until = 0.0

    def set_on(self, hold_s=None):
        with self._lock:
            self._state = "ON"
            self._hold_until = time.time() + float(hold_s) if hold_s and hold_s > 0 else 0.0

    def run(self):
        if not getattr(self, "_ok", False):
            return
        on = False
        try:
            while not self._stop.is_set():
                with self._lock:
                    st = self._state
                    hold_until = self._hold_until

                if st == "OFF":
                    self.GPIO.output(self.pin, self._lvl_off())
                    time.sleep(0.05)
                elif st == "ON":
                    self.GPIO.output(self.pin, self._lvl_on())
                    if hold_until > 0 and time.time() > hold_until:
                        self.set_off()
                    time.sleep(0.05)
                else:  # BLINK
                    on = not on
                    self.GPIO.output(self.pin, self._lvl_on() if on else self._lvl_off())
                    time.sleep(self.interval)
        finally:
            try: self.GPIO.output(self.pin, self._lvl_off())
            except Exception: pass

    def close(self):
        self._stop.set()
        try: self.join(timeout=1.0)
        except Exception: pass
        try: self.GPIO.output(self.pin, self._lvl_off())
        except Exception: pass

# ───────── 스트림 서버: JPEG over TCP ─────────
class StreamServer(threading.Thread):
    def __init__(self, host="0.0.0.0", port=9999, width=640, height=480, fps=15, jpeg_q=75, debug=False):
        super().__init__(daemon=True)
        self.addr=(host,port); self.w=int(width); self.h=int(height)
        self.dt=1.0/max(1,int(fps)); self.q=int(jpeg_q)
        self.debug=debug; self.stop=threading.Event(); self.sock=None; self.cam=None

    def run(self):
        self.cam=Picamera2()
        cfg=self.cam.create_preview_configuration(
            main={"size":(self.w,self.h),"format":"RGB888"},
            buffer_count=1, queue=False
        )
        self.cam.configure(cfg); self.cam.start(); time.sleep(0.4)

        self.sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        self.sock.bind(self.addr); self.sock.listen(1)
        print(f"[STREAM] listening on {self.addr[0]}:{self.addr[1]}")

        while not self.stop.is_set():
            try:
                conn,cli=self.sock.accept()
                conn.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                print(f"[STREAM] client connected: {cli}")
                self._loop(conn)
            except Exception as e:
                if not self.stop.is_set():
                    print("[STREAM] accept error:", e); time.sleep(0.5)

        try:self.cam.stop()
        except:pass
        try:self.sock.close()
        except:pass
        print("[STREAM] closed")

    def _loop(self,conn):
        try:
            cnt=0
            while not self.stop.is_set():
                t0=time.perf_counter()
                frame=self.cam.capture_array()
                frame=np.ascontiguousarray(frame)
                ok,jpg=cv2.imencode(".jpg",frame,[int(cv2.IMWRITE_JPEG_QUALITY), self.q])
                if not ok: continue
                data=jpg.tobytes()

                conn.sendall(struct.pack(">I",len(data)))
                conn.sendall(data)

                cnt+=1
                if cnt%100==0:
                    print(f"[STREAM] sent {cnt} frames (~{len(data)}B), q={self.q}")

                dt=time.perf_counter()-t0
                if dt<self.dt: time.sleep(self.dt-dt)
        except (BrokenPipeError,ConnectionResetError):
            print("[STREAM] client disconnected")
        finally:
            try: conn.close()
            except: pass

    def close(self): self.stop.set()

# ───────── 컨트롤 서버: JSON Lines ─────────
class ControlServer(threading.Thread):
    """
    정책:
      - RFID로 세션 시작 → window_s 동안 체크 (기본 10초)
      - 아두이노 수집 마감: ard_deadline = t_start + min(window_s, ard_cutoff_s)
      - 최종판정은 오직 '세션 deadline(=window_s 종료)' 시점 1회만 수행
      - PASS: 카메라 OK 누적 ≥ cam_need_n AND 아두이노 OK 누적 ≥ ard_need_n
              (아두이노 OK = gsr AND metal 동시 True 또는 OK=TRUE 라인)
      - LED: 측정 중 OFF 유지 → 최종판정에서 PASS면 ok_hold_s초 ON → 자동 OFF, FAIL이면 OFF 유지
    """
    def __init__(self, host="0.0.0.0", port=9998, default_window_s=10, server_url=None, gate_id="G1"):
        super().__init__(daemon=True)
        self.addr=(host,port)
        self.default_window_s=int(default_window_s)
        self.stop=threading.Event()
        self.sock=None
        self.conn=None
        self.lock=threading.Lock()

        self.session=dict(
            uid=None, deadline=0.0, badge_id=None, source=None,
            t_start=0.0, ard_deadline=0.0,
            cam_ok=None,
            ard_samples=[],        # [(t, gsr_ok, metal_ok), ...] (로그용)
            finalized=False,
            employee=None,
            cam_true_n=0,          # 카메라 OK 누적
            ard_tt_n=0             # 아두이노 True&True 누적
        )

        self.arduino=None
        self.led=None

        # 판정 파라미터
        self.ard_cutoff_s = 9.0
        self.ard_need_n   = 10
        self.cam_need_n   = 1
        self.ok_hold_s    = 2.0

        # 서버 연동 옵션
        self.server_url = server_url
        self.gate_id    = gate_id
        self.rfid_start_path    = "/api/rfid/start"
        self.rfid_complete_path = "/api/rfid/complete"
        self.rfid_resolve_path  = "/api/rfid/resolve"
        self.qr_complete_path   = "/api/qr/complete"   # ★ 추가

        # (옵션) SSL 검증 끄기: INSECURE_SSL=1 이면 verify=False
        self._verify_ssl = not str(os.environ.get("INSECURE_SSL","")).strip().lower() in ("1","true","y","yes")

        # 내부: 최종판정 타이머
        self._finalize_timer = None

    # 상태/연결
    def is_session_active(self):
        with self.lock:
            return (self.session.get("deadline", 0.0) > time.time()
                    and not self.session.get("finalized", False))

    def attach_arduino(self, ard_worker): self.arduino=ard_worker
    def attach_led(self, led): self.led=led  # RelayLED

    # 공통 요청 헬퍼 (로그 강화)
    def _req(self, method: str, path: str, **kwargs):
        if not self.server_url:
            return None
        try:
            import requests
            url = self.server_url.rstrip("/") + path
            kwargs.setdefault("timeout", 3)
            kwargs.setdefault("verify", self._verify_ssl)
            r = requests.request(method.upper(), url, **kwargs)
            body = ""
            try:
                body = r.text[:240]
            except Exception:
                body = "<no text>"
            print(f"[CTRL] {method.upper()} {path} → {r.status_code} {body}")
            return r
        except Exception as e:
            print(f"[CTRL] {method.upper()} {path} error:", e)
            return None

    # LED 표시
    def _show_result_led(self, ok: bool, duration_s: float | None = None):
        if not self.led:
            return
        hold = float(self.ok_hold_s if duration_s is None else duration_s)
        self.led.set_off(); time.sleep(0.05)
        if ok:
            self.led.set_on(hold_s=max(0.0, hold))

    # 스레드 본체
    def run(self):
        self.sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        self.sock.bind(self.addr); self.sock.listen(1)
        print(f"[CTRL] listening on {self.addr[0]}:{self.addr[1]}")
        threading.Thread(target=self._accept_loop, daemon=True).start()

        # 수동 트리거
        while not self.stop.is_set():
            try:
                s=input("Press ENTER to START(10s) or type seconds (e.g. 7): ").strip()
                if self.stop.is_set(): break
                window_s=self.default_window_s if s=="" else max(1,int(s))
                self.start_session(window_s=window_s, source="manual", badge_id=None)
            except (EOFError,KeyboardInterrupt):
                break
            except Exception as e:
                print("[CTRL] input err:",e)

        self.close(); print("[CTRL] closed")

    def _accept_loop(self):
        while not self.stop.is_set():
            try:
                conn,cli=self.sock.accept()
                conn.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
                with self.lock:
                    if self.conn:
                        try:self.conn.close()
                        except:pass
                    self.conn=conn
                print(f"[CTRL] client connected: {cli}")
                threading.Thread(target=self._rx_loop,args=(conn,),daemon=True).start()
            except Exception as e:
                if not self.stop.is_set():
                    print("[CTRL] accept error:",e); time.sleep(0.5)

    def _rx_loop(self, conn):
        buf=b""
        try:
            while not self.stop.is_set():
                data=conn.recv(4096)
                if not data:
                    print("[CTRL] client disconnected"); break
                buf+=data
                while b"\n" in buf:
                    line,buf=buf.split(b"\n",1)
                    try:
                        msg=json.loads(line.decode())
                    except Exception:
                        continue
                    if msg.get("cmd")=="det_result":
                        self.report_camera_result(bool(msg.get("ok")))
        finally:
            try: conn.close()
            except: pass

    # 서버 연동
    def _resolve_employee_server(self, uid:str):
        if not self.server_url: return None
        # GET 먼저, 실패하면 POST
        r = self._req("GET", self.rfid_resolve_path, params={"uid": uid})
        if not r or r.status_code != 200:
            r = self._req("POST", self.rfid_resolve_path, json={"uid": uid})
            if not r or r.status_code != 200:
                return None
        try:
            d = r.json()
            if isinstance(d, dict) and d.get("employeeId"):
                return d
        except Exception:
            pass
        return None

    def _notify_rfid_start(self, uid:str, employee:dict|None):
        if not self.server_url: return
        payload = {"gateId": self.gate_id, "rfidUid": uid}
        if isinstance(employee, dict):
            payload["employeeId"]   = employee.get("employeeId")
            payload["employeeName"] = employee.get("employeeName")
        self._req("POST", self.rfid_start_path, json=payload)

    # 세션 시작
    def start_session(self, window_s=None, source="rfid", badge_id=None):
        try:
            if self._finalize_timer: self._finalize_timer.cancel()
        except: pass

        window_s=int(window_s or self.default_window_s)
        uid=str(int(time.time()))
        t_start=time.time()

        employee=None
        if source == "rfid" and badge_id:
            employee=self._resolve_employee_server(badge_id)

        ard_deadline = t_start + min(window_s, float(self.ard_cutoff_s))
        deadline     = t_start + window_s

        with self.lock:
            self.session.update(
                uid=uid,
                t_start=t_start,
                deadline=deadline,
                ard_deadline=ard_deadline,
                badge_id=badge_id, source=source,
                cam_ok=None,
                ard_samples=[],
                finalized=False,
                employee=employee,
                cam_true_n=0,
                ard_tt_n=0
            )

        if self.led:
            self.led.set_off()

        delay = max(0.0, deadline - time.time())
        self._finalize_timer = threading.Timer(delay, lambda: self._maybe_finalize(trigger="timer"))
        self._finalize_timer.daemon = True
        self._finalize_timer.start()

        self._send_to_client({"cmd":"start","window_s":window_s,"uid":uid,"source":source,"badge_id":badge_id})
        if source == "rfid" and badge_id:
            self._notify_rfid_start(badge_id, employee)
        if self.arduino:
            self.arduino.start_measurement(window_s, uid)

        emp_id=(employee or {}).get("employeeId")
        print(f"[CTRL] START uid={uid} window={window_s}s source={source} badge={badge_id} emp={emp_id} "
              f"(final in {delay:.2f}s, ard_cutoff in {max(0.0, ard_deadline-time.time()):.2f}s)")

    def _send_to_client(self, payload:dict):
        with self.lock:
            if not self.conn:
                return
            try:
                self.conn.sendall((json.dumps(payload)+"\n").encode())
            except Exception as e:
                print("[CTRL] send error:",e)

    # 카메라 결과
    def report_camera_result(self, ok:bool):
        inc = 1 if ok else 0
        with self.lock:
            self.session["cam_ok"] = bool(ok)
            if inc:
                self.session["cam_true_n"] = int(self.session.get("cam_true_n",0)) + 1
                c = self.session["cam_true_n"]
            else:
                c = self.session.get("cam_true_n",0)
        print(f"[CTRL] camera: {ok} (cam_true_n={c})")

    # 아두이노 결과
    def report_arduino_result(self, gsr_ok:bool, metal_ok:bool):
        now=time.time()
        with self.lock:
            active = (self.session["deadline"] > now) and (not self.session["finalized"])
            within = (now <= self.session.get("ard_deadline", 0.0))
        if not active:
            print("[CTRL] arduino result ignored (no active session)")
            return
        if not within:
            print("[CTRL] arduino result ignored (after cutoff)")
            return

        with self.lock:
            self.session["ard_samples"].append((now, bool(gsr_ok), bool(metal_ok)))
            if gsr_ok and metal_ok:
                self.session["ard_tt_n"] = int(self.session.get("ard_tt_n",0)) + 1
                a = self.session["ard_tt_n"]
            else:
                a = self.session.get("ard_tt_n",0)
            n=len(self.session["ard_samples"])
        print(f"[CTRL] arduino sample #{n}: gsr={gsr_ok} metal={metal_ok} (ard_tt_n={a})")

    def report_arduino_final(self, ok: bool):
        if not ok:
            return
        now = time.time()
        with self.lock:
            active = (self.session["deadline"] > now) and (not self.session["finalized"])
            within = (now <= self.session.get("ard_deadline", 0.0))
            if not active:
                print("[CTRL] arduino FINAL ignored (no active session)")
                return
            if not within:
                print("[CTRL] arduino FINAL ignored (after cutoff)")
                return
            self.session["ard_tt_n"] = int(self.session.get("ard_tt_n", 0)) + 1
            a = self.session["ard_tt_n"]
        print(f"[CTRL] arduino FINAL OK (ard_tt_n={a})")

    # 최종판정
    def _maybe_finalize(self, trigger:str|None=None):
        with self.lock:
            if self.session.get("finalized"):
                return
            now = time.time()
            if not (now >= self.session.get("deadline",0.0) > 0):
                return

            cam_need = int(self.cam_need_n)
            ard_need = int(self.ard_need_n)
            cam_cnt  = int(self.session.get("cam_true_n",0))
            ard_cnt  = int(self.session.get("ard_tt_n",0))

        cam_ok   = (cam_cnt >= cam_need)
        ard_ok   = (ard_cnt >= ard_need)
        final_ok = cam_ok and ard_ok

        print(f"[CTRL] FINAL[{trigger}] uid={self.session['uid']} "
              f"=> cam_cnt={cam_cnt}/{cam_need}, ard_cnt={ard_cnt}/{ard_need} "
              f"=> cam_ok={cam_ok} arduino_ok={ard_ok} => PASS={final_ok}")

        # LED
        self._show_result_led(final_ok, duration_s=self.ok_hold_s)

        # 서버 보고
        try:
            if self.server_url:
                # 메탈 감지 (샘플 중 하나라도 True면 True)
                metal_any = any(m for _, g, m in self.session.get("ard_samples", []))

                if self.session.get("source") == "rfid":
                    payload = {
                        "gateId": self.gate_id,
                        "rfidUid": self.session.get("badge_id"),
                        "ok": final_ok,
                        "helmet": cam_ok,
                        "metal": metal_any,
                    }
                    self._req("POST", self.rfid_complete_path, json=payload)

                elif self.session.get("source") == "qr":
                    payload = {
                        "ticketId": self.session.get("badge_id"),  # QR은 badge_id에 ticketId 저장됨
                        "gateId": self.gate_id,
                        "ok": final_ok,
                        "helmet": cam_ok,
                        "metal": metal_any,
                        "source": "Gate",
                    }
                    self._req("POST", self.qr_complete_path, json=payload)
        except Exception as e:
            print("[CTRL] report to server failed:", e)

        # 세션 종료 플래그/타이머 정리
        with self.lock:
            self.session["finalized"]=True
            try:
                if self._finalize_timer: self._finalize_timer.cancel()
            except: pass

    def close(self):
        self.stop.set()
        try:self.sock.close()
        except:pass
        with self.lock:
            try:self.conn.close()
            except:pass
        try:
            if self._finalize_timer: self._finalize_timer.cancel()
        except: pass


# ───────── 아두이노 워커 ─────────
class ArduinoWorker(threading.Thread):
    def __init__(self, ctrl: ControlServer,
                 port="/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_054433A4937351A0A7F5-if00",
                 baud=9600, debug=False):
        super().__init__(daemon=True)
        self.ctrl=ctrl; self.port=port; self.baud=int(baud); self.debug=debug
        self.stop=threading.Event(); self.ser=None

    def _open_serial(self):
        import serial, time
        ser = serial.Serial(
            self.port, self.baud,
            timeout=0.2, write_timeout=0.5,
            rtscts=False, dsrdtr=False, xonxoff=False,
            exclusive=True
        )
        try:
            ser.setDTR(False); ser.setRTS(False)
        except Exception:
            pass
        time.sleep(2.0)
        try: ser.reset_input_buffer()
        except Exception: pass
        return ser

    def run(self):
        import time, json, serial
        while not self.stop.is_set():
            try:
                self.ser = self._open_serial()
                print(f"[ARD] open {self.port}@{self.baud}")
                buf = b""
                while not self.stop.is_set():
                    chunk = self.ser.read(256)
                    if not chunk:
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        txt = line.decode(errors="ignore").strip().replace("\r", "")
                        if not txt:
                            continue
                        if self.debug:
                            print("[ARD] RX:", txt)

                        # 라인 파싱 (대/소문자 무시, JSON 지원)
                        gsr_ok, metal_ok, ok_flag = self._parse_result_line(txt)

                        # 활성 세션 여부 체크
                        session_active = self.ctrl.is_session_active()

                        if ok_flag is True:
                            if session_active:
                                self.ctrl.report_arduino_final(True)   # 최종 OK 1회 누적
                            elif self.debug:
                                print("[ARD] OK=True but no active session → ignored")
                        elif (gsr_ok is not None) and (metal_ok is not None):
                            if session_active:
                                self.ctrl.report_arduino_result(gsr_ok, metal_ok)
                            else:
                                if self.debug:
                                    print("[ARD] sample parsed but no active session → ignored")
                        else:
                            # 정보 부족 라인은 무시
                            pass
            except Exception as e:
                print("[ARD] serial error:", e)
                time.sleep(1.0)
            finally:
                try:
                    self.ser and self.ser.close()
                except:
                    pass
        print("[ARD] closed")

    def _as_bool(self, v):
        if isinstance(v, bool): return v
        if isinstance(v, (int, float)): return v != 0
        if isinstance(v, str):
            return v.strip().lower() in ("1","true","t","y","yes","on")
        return False

    def _parse_result_line(self, txt:str):
        # ① JSON 라인 {"gsr":..,"metal":..,"ok":..}
        if txt.startswith("{") and txt.endswith("}"):
            try:
                d=json.loads(txt)
                g=self._as_bool(d.get("gsr", False))
                m=self._as_bool(d.get("metal", False))
                ok_flag=self._as_bool(d.get("ok", False))
                return g, m, ok_flag
            except Exception:
                return None, None, None

        # ② 비JSON: 대소문자 무시 key=value 추출 (예: "... GSR=516 ... METAL=1 ... OK=TRUE")
        low = txt.strip().lower()
        tokens = dict(re.findall(r'([a-z]+)\s*=\s*([a-z0-9\.\-\_]+)', low))
        gsr_raw = tokens.get('gsr')
        metal_v = tokens.get('metal')
        ok_v    = tokens.get('ok')

        gsr_ok = None
        metal_ok = self._as_bool(metal_v) if metal_v is not None else None
        ok_flag  = self._as_bool(ok_v) if ok_v is not None else None
        return gsr_ok, metal_ok, ok_flag

    def start_measurement(self, secs:int, uid:str):
        if not self.ser:
            print("[ARD] not ready"); return
        cmd=f"START {int(secs)}\n"
        try:
            self.ser.write(cmd.encode()); self.ser.flush()
            print(f"[ARD] TX:{cmd.strip()}")
        except Exception as e:
            print("[ARD] write err:",e)

    def close(self): self.stop.set()

# ───────── RFID 워커(선택) ─────────
class RFIDWorker(threading.Thread):
    def __init__(self, ctrl:ControlServer, mode="none", serial_port="/dev/ttyUSB0", baud=115200,
                 window_s=10, allowlist=None, cooldown_s=2.0, debug=False):
        super().__init__(daemon=True)
        self.ctrl=ctrl; self.mode=mode.lower(); self.port=serial_port; self.baud=int(baud)
        self.window_s=int(window_s); self.allow=set(allowlist or []); self.cooldown=float(cooldown_s)
        self.debug=debug; self.stop=threading.Event(); self.last={}

    def run(self):
        if self.mode=="mfrc522":
            self._run_mfrc522()
        elif self.mode=="serial":
            self._run_serial()
        else:
            print("[RFID] disabled")

    def _trigger(self, uid:str):
        now=time.time()
        if uid in self.last and now-self.last[uid]<self.cooldown:
            return
        self.last[uid]=now
        if self.allow and uid not in self.allow:
            print(f"[RFID] deny {uid}"); return
        print(f"[RFID] OK {uid} -> start {self.window_s}s")
        self.ctrl.start_session(window_s=self.window_s, source="rfid", badge_id=uid)

    def _run_mfrc522(self):
        try:
            from mfrc522 import SimpleMFRC522; import RPi.GPIO as GPIO
        except Exception as e:
            print("[RFID] mfrc522 import failed:",e); return
        reader=SimpleMFRC522(); print("[RFID] MFRC522 ready")
        try:
            while not self.stop.is_set():
                uid=reader.read_id_no_block()
                if uid: self._trigger(str(uid))
                time.sleep(0.1)
        finally:
            try: GPIO.cleanup()
            except: pass

    def _run_serial(self):
        try:
            import serial
            ser=serial.Serial(self.port,self.baud,timeout=0.2)
        except Exception as e:
            print("[RFID] serial open failed:",e); return
        print(f"[RFID] serial on {self.port}@{self.baud}")
        buf=b""
        try:
            while not self.stop.is_set():
                chunk=ser.read(256)
                if not chunk: continue
                buf+=chunk
                while b"\n" in buf:
                    line,buf=buf.split(b"\n",1)
                    uid=line.decode(errors="ignore").strip().replace("\r","")
                    if uid: self._trigger(uid)
        finally:
            try: ser.close()
            except: pass

    def close(self): self.stop.set()

class QRTaskPoller(threading.Thread):
    def __init__(self, ctrl: ControlServer, period_s=1.0):
        super().__init__(daemon=True)
        self.ctrl = ctrl
        self.period = float(period_s)
        self.stop = threading.Event()

    def run(self):
        if not self.ctrl.server_url:
            print("[QR] server_url not set. QR disabled.")
            return
        import requests
        while not self.stop.is_set():
            try:
                if not self.ctrl.is_session_active():
                    url = self.ctrl.server_url.rstrip('/') + f"/api/qr/next_task?gate={self.ctrl.gate_id}"
                    r = requests.get(url, timeout=2)
                    if r.status_code == 200:
                        t = r.json()
                        tid = t.get("ticketId")
                        if tid:
                            print(f"[QR] got ticket {tid} → start verification")
                            self.ctrl.start_session(window_s=self.ctrl.default_window_s,
                                                    source="qr", badge_id=tid)
            except Exception as e:
                print("[QR] poll error:", e)
            time.sleep(self.period)

    def close(self): self.stop.set()

# ───────── 메인 ─────────
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--stream-port", type=int, default=9999)
    ap.add_argument("--ctrl-port",   type=int, default=9998)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--jpeg-q", type=int, default=75)
    ap.add_argument("--window-s", type=int, default=10)
    ap.add_argument("--debug", action="store_true")

    # 서버/게이트 설정
    ap.add_argument("--server-url", default=os.environ.get("SERVER_URL","http://127.0.0.1:5001"))
    ap.add_argument("--gate-id",    default=os.environ.get("GATE_ID","G1"))

    # RFID
    ap.add_argument("--rfid", default="none", choices=["none","mfrc522","serial"])
    ap.add_argument("--rfid-window-s", type=int, default=10)
    ap.add_argument("--rfid-serial-port", default="/dev/ttyUSB0")
    ap.add_argument("--rfid-baud", type=int, default=115200)
    ap.add_argument("--rfid-allow", default=None, help="JSON array file of allowed UIDs")
    ap.add_argument("--rfid-cooldown", type=float, default=2.0)

    # Arduino
    ap.add_argument("--arduino-port",
        default="/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_054433A4937351A0A7F5-if00")
    ap.add_argument("--arduino-baud", type=int, default=9600)

    # GPIO Relay/LED
    ap.add_argument("--gpio-mode", default="board", choices=["board","bcm"])
    ap.add_argument("--gpio-pin", type=int, default=16)
    ap.add_argument("--gpio-active-low", action="store_true")
    ap.add_argument("--blink-s", type=float, default=0.4)
    ap.add_argument("--ok-hold-s", type=float, default=2.0)  # PASS 점등 시간

    # Arduino cutoff/누적 개수 옵션
    ap.add_argument("--ard-cutoff-s", type=float, default=9.0)
    ap.add_argument("--ard-need-n", type=int, default=10)     # ★ 기본 10으로 변경

    # 서버 엔드포인트 경로(서버와 동일하게!)
    ap.add_argument("--rfid-start-path", default="/api/rfid/start")
    ap.add_argument("--rfid-complete-path", default="/api/rfid/complete")
    ap.add_argument("--rfid-resolve-path", default="/api/rfid/resolve")

    args=ap.parse_args()

    # allowlist 로드(옵션)
    allow=[]
    if args.rfid_allow and os.path.exists(args.rfid_allow):
        try:
            allow=json.load(open(args.rfid_allow, "r", encoding="utf-8"))
        except Exception as e:
            print("[MAIN] allowlist load err:",e)

    stop_all=threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_all.set())

    stream=StreamServer(args.host,args.stream_port,args.width,args.height,args.fps,args.jpeg_q,args.debug)
    ctrl=ControlServer(args.host,args.ctrl_port,args.window_s,
                       server_url=args.server_url, gate_id=args.gate_id)
    ctrl.ard_cutoff_s = float(args.ard_cutoff_s)
    ctrl.ard_need_n   = int(args.ard_need_n)
    ctrl.rfid_start_path    = args.rfid_start_path
    ctrl.rfid_complete_path = args.rfid_complete_path
    ctrl.rfid_resolve_path  = args.rfid_resolve_path
    ctrl.ok_hold_s          = float(args.ok_hold_s)

    ard=ArduinoWorker(ctrl, port=args.arduino_port, baud=args.arduino_baud, debug=args.debug)
    rfid=RFIDWorker(ctrl, mode=args.rfid, serial_port=args.rfid_serial_port, baud=args.rfid_baud,
                    window_s=args.rfid_window_s, allowlist=allow, cooldown_s=args.rfid_cooldown, debug=args.debug)
    led=RelayLED(gpio_mode=args.gpio_mode, gpio_pin=args.gpio_pin,
                 active_low=args.gpio_active_low, interval_s=args.blink_s)
    qrpoll = QRTaskPoller(ctrl, period_s=1.0)

    ctrl.attach_arduino(ard)
    ctrl.attach_led(led)

    # 시작
    stream.start(); ctrl.start(); ard.start(); rfid.start(); led.start(); qrpoll.start()
    print("[MAIN] servers up. Ctrl+C to stop.")
    try:
        while not stop_all.is_set():
            time.sleep(0.3)
    finally:
        stream.close(); ctrl.close(); ard.close(); rfid.close(); led.close(); qrpoll.close()
        print("[MAIN] exit")

if __name__ == "__main__":
    main()
