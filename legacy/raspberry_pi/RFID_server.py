#!/usr/bin/env python3
import socket, struct, time, sys, cv2, numpy as np, argparse, json, threading, signal, os
from picamera2 import Picamera2

# ───────── 스트림 서버: JPEG over TCP (port: --stream-port) ─────────
class StreamServer(threading.Thread):
    def __init__(self, host="0.0.0.0", port=9999, width=640, height=480, fps=15, jpeg_q=75, debug=False):
        super().__init__(daemon=True)
        self.addr=(host,port); self.w=int(width); self.h=int(height)
        self.dt=1.0/max(1,int(fps)); self.q=int(jpeg_q)
        self.debug=debug; self.stop=threading.Event(); self.sock=None; self.cam=None

    def run(self):
        # Camera
        self.cam=Picamera2()
        cfg=self.cam.create_preview_configuration(
            main={"size":(self.w,self.h),"format":"RGB888"},
            buffer_count=1, queue=False
        )
        self.cam.configure(cfg); self.cam.start(); time.sleep(0.4)

        # Socket
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

        # Cleanup
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
                frame=self.cam.capture_array()                 # RGB888
                frame=np.ascontiguousarray(frame)              # stripe 방지
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

# ───────── 컨트롤 서버: JSON Lines (port: --ctrl-port) ─────────
class ControlServer(threading.Thread):
    def __init__(self, host="0.0.0.0", port=9998, default_window_s=10):
        super().__init__(daemon=True)
        self.addr=(host,port); self.default_window_s=int(default_window_s)
        self.stop=threading.Event(); self.sock=None; self.conn=None; self.lock=threading.Lock()
        self.session=dict(uid=None, deadline=0.0, badge_id=None, source=None,
                          cam_ok=None, gsr_ok=None, metal_ok=None)
        self.arduino=None  # set later

    def attach_arduino(self, ard_worker): self.arduino=ard_worker

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

    # 세션 시작: 클라에 start, 아두이노에 START <sec>
    def start_session(self, window_s=None, source="rfid", badge_id=None):
        window_s=int(window_s or self.default_window_s)
        uid=str(int(time.time()))
        with self.lock:
            self.session.update(uid=uid, deadline=time.time()+window_s,
                                badge_id=badge_id, source=source,
                                cam_ok=None, gsr_ok=None, metal_ok=None)
        self._send_to_client({"cmd":"start","window_s":window_s,"uid":uid,"source":source,"badge_id":badge_id})
        if self.arduino:
            self.arduino.start_measurement(window_s, uid)
        print(f"[CTRL] START uid={uid} window={window_s}s source={source} badge={badge_id}")

    def _send_to_client(self, payload:dict):
        with self.lock:
            if not self.conn:
                print("[CTRL] no client yet"); return
            try:
                self.conn.sendall((json.dumps(payload)+"\n").encode())
            except Exception as e:
                print("[CTRL] send error:",e)

    # 결과 취합
    def report_camera_result(self, ok:bool):
        with self.lock:
            self.session["cam_ok"]=bool(ok)
        print(f"[CTRL] camera: {ok}")
        self._maybe_finalize()

    def report_arduino_result(self, gsr_ok:bool, metal_ok:bool):
        with self.lock:
            self.session["gsr_ok"]=bool(gsr_ok); self.session["metal_ok"]=bool(metal_ok)
        print(f"[CTRL] arduino: gsr={gsr_ok} metal={metal_ok}")
        self._maybe_finalize()

    def _maybe_finalize(self):
        with self.lock:
            s=self.session.copy()
        now=time.time()
        timed_out = (now > s["deadline"]>0)
        have_cam = (s["cam_ok"] is not None)
        have_ard = (s["gsr_ok"] is not None and s["metal_ok"] is not None)
        if timed_out or (have_cam and have_ard):
            cam_ok   = bool(s["cam_ok"]) if s["cam_ok"] is not None else False
            gsr_ok   = bool(s["gsr_ok"]) if s["gsr_ok"] is not None else False
            metal_ok = bool(s["metal_ok"]) if s["metal_ok"] is not None else False
            final_ok = cam_ok and gsr_ok and metal_ok
            print(f"[CTRL] FINAL uid={s['uid']} => cam={cam_ok} gsr={gsr_ok} metal={metal_ok} => PASS={final_ok}")
            # TODO: GPIO 릴레이/부저/로그 저장 등 추가 위치
            # 세션 종료
            with self.lock:
                self.session["deadline"]=0.0

    def close(self):
        self.stop.set()
        try:self.sock.close()
        except:pass
        with self.lock:
            try:self.conn.close()
            except:pass

# ───────── 아두이노 워커: /dev/ttyACM0에 START/RESULT 프로토콜 ─────────
class ArduinoWorker(threading.Thread):
    """
    Arduino 스케치 프로토콜(권장):
      Pi -> Arduino :  START <secs>\n        예) START 10
      Arduino -> Pi :  {"gsr":1,"metal":1,"ok":1}\n  (측정 종료 시 1줄 JSON)
    JSON이 힘들면:  RESULT gsr=1 metal=0\n  형태도 허용.
    """
    def __init__(self, ctrl: ControlServer, port="/dev/ttyACM0", baud=9600, debug=False):
        super().__init__(daemon=True)
        self.ctrl=ctrl; self.port=port; self.baud=int(baud); self.debug=debug
        self.stop=threading.Event(); self.ser=None
        self.pending_uid=None

    def run(self):
        try:
            import serial
            self.ser=serial.Serial(self.port,self.baud,timeout=0.2)
            print(f"[ARD] open {self.port}@{self.baud}")
        except Exception as e:
            print("[ARD] serial open failed:",e); return

        buf=b""
        while not self.stop.is_set():
            try:
                chunk=self.ser.read(256)
                if not chunk: continue
                buf+=chunk
                while b"\n" in buf:
                    line,buf=buf.split(b"\n",1)
                    txt=line.decode(errors="ignore").strip().replace("\r","")
                    if not txt: continue
                    if self.debug: print("[ARD] RX:",txt)
                    gsr_ok, metal_ok = self._parse_result_line(txt)
                    if gsr_ok is not None and metal_ok is not None:
                        self.ctrl.report_arduino_result(gsr_ok, metal_ok)
            except Exception as e:
                if self.debug: print("[ARD] err:",e); time.sleep(0.2)

        try: self.ser.close()
        except: pass
        print("[ARD] closed")

    def _parse_result_line(self, txt:str):
        # JSON 우선
        if txt.startswith("{") and txt.endswith("}"):
            try:
                d=json.loads(txt)
                g=bool(int(d.get("gsr",0)))
                m=bool(int(d.get("metal",0)))
                return g,m
            except Exception:
                return None,None
        # 키=값 파싱: RESULT gsr=1 metal=0
        if "gsr=" in txt and "metal=" in txt:
            try:
                parts=dict(kv.split("=",1) for kv in txt.replace("RESULT","").split() if "=" in kv)
                g=bool(int(parts.get("gsr","0"))); m=bool(int(parts.get("metal","0")))
                return g,m
            except Exception:
                return None,None
        # 그 외(센서 디버그 로그 등)는 무시
        return None,None

    def start_measurement(self, secs:int, uid:str):
        if not self.ser:
            print("[ARD] not ready"); return
        self.pending_uid=uid
        cmd=f"START {int(secs)}\n"
        try:
            self.ser.write(cmd.encode()); self.ser.flush()
            print(f"[ARD] TX:{cmd.strip()}")
        except Exception as e:
            print("[ARD] write err:",e)

    def close(self): self.stop.set()

# ───────── RFID 워커(선택): mfrc522 또는 serial UID ─────────
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

    # RFID
    ap.add_argument("--rfid", default="none", choices=["none","mfrc522","serial"])
    ap.add_argument("--rfid-window-s", type=int, default=10)
    ap.add_argument("--rfid-serial-port", default="/dev/ttyUSB0")
    ap.add_argument("--rfid-baud", type=int, default=115200)
    ap.add_argument("--rfid-allow", default=None, help="JSON array file of allowed UIDs")
    ap.add_argument("--rfid-cooldown", type=float, default=2.0)

    # Arduino
    ap.add_argument("--arduino-port", default="/dev/ttyACM0")
    ap.add_argument("--arduino-baud", type=int, default=9600)  # ← 9600으로 변경

    args=ap.parse_args()

    # allowlist 로드
    allow=[]
    if args.rfid_allow and os.path.exists(args.rfid_allow):
        try:
            allow=json.load(open(args.rfid_allow, "r", encoding="utf-8"))
        except Exception as e:
            print("[MAIN] allowlist load err:",e)

    stop_all=threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_all.set())

    stream=StreamServer(args.host,args.stream_port,args.width,args.height,args.fps,args.jpeg_q,args.debug)
    ctrl=ControlServer(args.host,args.ctrl_port,args.window_s)
    ard=ArduinoWorker(ctrl, port=args.arduino_port, baud=args.arduino_baud, debug=args.debug)
    rfid=RFIDWorker(ctrl, mode=args.rfid, serial_port=args.rfid_serial_port, baud=args.rfid_baud,
                    window_s=args.rfid_window_s, allowlist=allow, cooldown_s=args.rfid_cooldown, debug=args.debug)

    ctrl.attach_arduino(ard)

    stream.start(); ctrl.start(); ard.start(); rfid.start()
    print("[MAIN] servers up. Ctrl+C to stop.")
    try:
        while not stop_all.is_set():
            time.sleep(0.3)
    finally:
        stream.close(); ctrl.close(); ard.close(); rfid.close()

if __name__ == "__main__":
    main()
