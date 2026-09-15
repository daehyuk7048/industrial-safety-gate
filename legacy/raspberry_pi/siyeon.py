#!/usr/bin/env python3
import socket, struct, time, cv2, numpy as np, argparse, json, threading, signal, os
from picamera2 import Picamera2

# ───────── GPIO Relay/LED ─────────
class RelayLED(threading.Thread):
    def __init__(self, gpio_mode="board", gpio_pin=16, active_low=False, interval_s=0.4):
        super().__init__(daemon=True)
        self.gpio_mode = gpio_mode.lower()
        self.pin = int(gpio_pin)
        self.active_low = bool(active_low)
        self.interval = float(interval_s)
        self._state = "OFF"          # 기본 꺼짐
        self._hold_until = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        try:
            import RPi.GPIO as GPIO
            self.GPIO = GPIO
            if self.GPIO.getmode() is None:
                self.GPIO.setwarnings(False)
                if self.gpio_mode == "bcm": self.GPIO.setmode(self.GPIO.BCM)
                else: self.GPIO.setmode(self.GPIO.BOARD)
            self.GPIO.setup(self.pin, self.GPIO.OUT, initial=self._lvl_off())
            self._ok = True
        except Exception as e:
            print("[GPIO] init failed:", e); self._ok = False

    def _lvl_on(self):  return self.GPIO.LOW if self.active_low else self.GPIO.HIGH
    def _lvl_off(self): return self.GPIO.HIGH if self.active_low else self.GPIO.LOW

    def set_off(self):
        with self._lock: self._state="OFF"; self._hold_until=0.0
    def set_blink(self):  # 필요 시 사용
        with self._lock: self._state="BLINK"; self._hold_until=0.0
    def set_on(self, hold_s=None):
        with self._lock:
            self._state="ON"
            self._hold_until = time.time()+float(hold_s) if hold_s and hold_s>0 else 0.0

    def run(self):
        if not getattr(self,"_ok",False): return
        on=False
        try:
            while not self._stop.is_set():
                with self._lock:
                    st=self._state; hold_until=self._hold_until
                if st=="OFF":
                    self.GPIO.output(self.pin, self._lvl_off()); time.sleep(0.05)
                elif st=="ON":
                    self.GPIO.output(self.pin, self._lvl_on())
                    if hold_until>0 and time.time()>hold_until: self.set_off()  # 2초 뒤 자동 꺼짐
                    time.sleep(0.05)
                else:  # BLINK (현재 기본 사용 안 함)
                    on=not on
                    self.GPIO.output(self.pin, self._lvl_on() if on else self._lvl_off())
                    time.sleep(self.interval)
        finally:
            try: self.GPIO.output(self.pin, self._lvl_off())
            except: pass

    def close(self):
        self._stop.set()
        try: self.join(timeout=1.0)
        except: pass
        try: self.GPIO.output(self.pin, self._lvl_off())
        except: pass

# ───────── JPEG Stream over TCP ─────────
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
        try:
            self.cam.stop(); self.sock.close()
        except: pass
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
                conn.sendall(struct.pack(">I",len(data))); conn.sendall(data)
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

# ───────── Control Server (CAMERA-ONLY 판정 & 10초 후 단 한 줄 출력) ─────────
class ControlServer(threading.Thread):
    def __init__(self, host="0.0.0.0", port=9998, default_window_s=10):
        super().__init__(daemon=True)
        self.addr=(host,port); self.default_window_s=int(default_window_s)
        self.stop=threading.Event(); self.sock=None; self.conn=None; self.lock=threading.Lock()
        self.session=dict(uid=None, deadline=0.0, badge_id=None, source=None,
                          cam_seen_ok=False, finalized=False)  # cam_seen_ok: 윈도우 내 한 번이라도 OK 봤는가
        self.arduino=None
        self.led=None

    def attach_arduino(self, ard_worker): self.arduino=ard_worker
    def attach_led(self, led: RelayLED): self.led=led

    def run(self):
        self.sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        self.sock.bind(self.addr); self.sock.listen(1)
        print(f"[CTRL] listening on {self.addr[0]}:{self.addr[1]}")
        threading.Thread(target=self._accept_loop, daemon=True).start()

        # 수동 트리거(Enter)
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
                if not data: break
                buf+=data
                while b"\n" in buf:
                    line,buf=buf.split(b"\n",1)
                    try:
                        msg=json.loads(line.decode())
                    except Exception:
                        continue
                    if msg.get("cmd")=="det_result":
                        # 윈도우 안에서 OK가 한 번이라도 오면 cam_seen_ok=True로 래치
                        if bool(msg.get("ok")):
                            with self.lock:
                                self.session["cam_seen_ok"]=True
        finally:
            try: conn.close()
            except: pass

    def start_session(self, window_s=None, source="rfid", badge_id=None):
        window_s=int(window_s or self.default_window_s)
        uid=str(int(time.time()))
        with self.lock:
            self.session.update(uid=uid, deadline=time.time()+window_s,
                                badge_id=badge_id, source=source,
                                cam_seen_ok=False, finalized=False)
        # 시작 시 LED는 꺼짐 유지
        if self.led: self.led.set_off()
        # 클라이언트에 윈도우 시작 알림
        self._send_to_client({"cmd":"start","window_s":window_s,"uid":uid,"source":source,"badge_id":badge_id})
        # 아두이노에는 START만 던지고 조용히(수신 로그/판정 무시)
        if self.arduino: self.arduino.start_measurement(window_s, uid)
        print(f"[CTRL] START uid={uid} window={window_s}s source={source} badge={badge_id}")

        # 타임아웃 감시 쓰레드
        threading.Thread(target=self._watchdog, args=(uid,), daemon=True).start()

    def _send_to_client(self, payload:dict):
        with self.lock:
            if not self.conn:
                print("[CTRL] no client yet"); return
            try:
                self.conn.sendall((json.dumps(payload)+"\n").encode())
            except Exception as e:
                print("[CTRL] send error:",e)

    def _watchdog(self, uid:str):
        # 윈도우가 끝날 때까지 기다렸다가 딱 한 번 최종 판정
        while not self.stop.is_set():
            with self.lock:
                if self.session["uid"] != uid: return  # 다른 세션으로 바뀜
                left = self.session["deadline"] - time.time()
                finalized = self.session["finalized"]
            if finalized: return
            if left <= 0: break
            time.sleep(min(0.05, max(0.0, left/5)))

        with self.lock:
            s = self.session.copy()
            if s["finalized"]: return
            final_ok = bool(s["cam_seen_ok"])
            # 최종 한 줄만 출력 (요청대로 TRUE/FALSE만)
            print("TRUE" if final_ok else "FALSE")
            # LED: TRUE면 2초 점등 후 자동 꺼짐, FALSE면 꺼짐 유지
            if self.led:
                if final_ok: self.led.set_on(hold_s=2.0)
                else: self.led.set_off()
            self.session["finalized"] = True
            self.session["deadline"] = 0.0

    def close(self):
        self.stop.set()
        try:self.sock.close()
        except:pass
        with self.lock:
            try:self.conn.close()
            except:pass

# ───────── Arduino Worker (START만 송신, 수신/로그 무시) ─────────
class ArduinoWorker(threading.Thread):
    def __init__(self, ctrl: ControlServer, port="/dev/ttyACM0", baud=9600, debug=False):
        super().__init__(daemon=True)
        self.ctrl=ctrl; self.port=port; self.baud=int(baud); self.debug=debug
        self.stop=threading.Event(); self.ser=None

    def _open_serial(self):
        import serial, time
        ser = serial.Serial(self.port, self.baud, timeout=0.2, write_timeout=0.5,
                            rtscts=False, dsrdtr=False, xonxoff=False, exclusive=True)
        try: ser.setDTR(False); ser.setRTS(False)
        except: pass
        time.sleep(2.0)
        try: ser.reset_input_buffer()
        except: pass
        return ser

    def run(self):
        try:
            import serial
            self.ser = self._open_serial()
            print(f"[ARD] open {self.port}@{self.baud}")
            while not self.stop.is_set():
                try:
                    self.ser.read(256)  # 버퍼만 소비 (아무 로그도 남기지 않음)
                except Exception:
                    time.sleep(0.2)
        except Exception as e:
            print("[ARD] serial error:", e)
        finally:
            try: self.ser and self.ser.close()
            except: pass
        print("[ARD] closed")

    def start_measurement(self, secs:int, uid:str):
        if not self.ser: return
        cmd=f"START {int(secs)}\n"
        try:
            self.ser.write(cmd.encode()); self.ser.flush()
        except Exception as e:
            print("[ARD] write err:", e)

    def close(self): self.stop.set()

# ───────── RFID Worker (옵션) ─────────
class RFIDWorker(threading.Thread):
    def __init__(self, ctrl:ControlServer, mode="none", serial_port="/dev/ttyUSB0", baud=115200,
                 window_s=10, allowlist=None, cooldown_s=2.0, debug=False):
        super().__init__(daemon=True)
        self.ctrl=ctrl; self.mode=mode.lower(); self.port=serial_port; self.baud=int(baud)
        self.window_s=int(window_s); self.allow=set(allowlist or []); self.cooldown=float(cooldown_s)
        self.debug=debug; self.stop=threading.Event(); self.last={}

    def run(self):
        if self.mode=="mfrc522": self._run_mfrc522()
        elif self.mode=="serial": self._run_serial()
        else: print("[RFID] disabled")

    def _trigger(self, uid:str):
        now=time.time()
        if uid in self.last and now-self.last[uid]<self.cooldown: return
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

# ───────── main ─────────
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

    # RFID
    ap.add_argument("--rfid", default="none", choices=["none","mfrc522","serial"])
    ap.add_argument("--rfid-window-s", type=int, default=10)
    ap.add_argument("--rfid-serial-port", default="/dev/ttyUSB0")
    ap.add_argument("--rfid-baud", type=int, default=115200)
    ap.add_argument("--rfid-allow", default=None)
    ap.add_argument("--rfid-cooldown", type=float, default=2.0)

    # Arduino
    ap.add_argument("--arduino-port", default="/dev/ttyACM0")
    ap.add_argument("--arduino-baud", type=int, default=9600)

    # GPIO Relay/LED (물리 핀 16)
    ap.add_argument("--gpio-mode", default="board", choices=["board","bcm"])
    ap.add_argument("--gpio-pin", type=int, default=16)
    ap.add_argument("--gpio-active-low", action="store_true")
    ap.add_argument("--blink-s", type=float, default=0.4)

    args=ap.parse_args()

    # RFID allowlist
    allow=[]
    if args.rfid_allow and os.path.exists(args.rfid_allow):
        try: allow=json.load(open(args.rfid_allow,"r",encoding="utf-8"))
        except Exception as e: print("[MAIN] allowlist load err:",e)

    stop_all=threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_all.set())

    # 모듈
    stream=StreamServer(args.host,args.stream_port,args.width,args.height,args.fps,args.jpeg_q,args.debug)
    ctrl=ControlServer(args.host,args.ctrl_port,args.window_s)
    ard=ArduinoWorker(ctrl, port=args.arduino_port, baud=args.arduino_baud, debug=False)
    rfid=RFIDWorker(ctrl, mode=args.rfid, serial_port=args.rfid_serial_port, baud=args.rfid_baud,
                    window_s=args.rfid_window_s, allowlist=allow, cooldown_s=args.rfid_cooldown, debug=args.debug)
    led=RelayLED(gpio_mode=args.gpio_mode, gpio_pin=args.gpio_pin,
                 active_low=args.gpio_active_low, interval_s=args.blink_s)

    ctrl.attach_arduino(ard)
    ctrl.attach_led(led)

    # 시작
    stream.start(); ctrl.start(); ard.start(); rfid.start(); led.start()
    print("[MAIN] servers up. Ctrl+C to stop.")
    try:
        while not stop_all.is_set(): time.sleep(0.3)
    finally:
        stream.close(); ctrl.close(); ard.close(); rfid.close(); led.close()
        print("[MAIN] exit")

if __name__ == "__main__":
    main()
