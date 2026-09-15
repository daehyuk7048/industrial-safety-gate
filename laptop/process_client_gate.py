#!/usr/bin/env python3
import socket, struct, time, cv2, numpy as np, argparse, json, threading
from ultralytics import YOLO

def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        pkt = sock.recv(n - len(buf))
        if not pkt:
            return None
        buf += pkt
    return buf

class GateClient:
    def __init__(
        self, pi_ip, stream_port=9999, ctrl_port=9998,
        model_path="best.pt", conf=0.5, iou=0.45, imgsz=640,
        skip=0, drop_backlog=True, recvbuf_kb=256,
        window_w=960, window_h=540
    ):
        # ── stream socket ──────────────────────────────────────────────────
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if recvbuf_kb > 0:
            self.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, recvbuf_kb * 1024)
        self.s.connect((pi_ip, stream_port))
        self.buf = b""
        self.HDR = struct.calcsize(">I")
        self.CHUNK = 65536

        # ── control socket ─────────────────────────────────────────────────
        self.c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.c.connect((pi_ip, ctrl_port))

        # ── YOLO (CPU) ─────────────────────────────────────────────────────
        self.model = YOLO(model_path)
        self.infer_kwargs = dict(conf=conf, iou=iou, imgsz=imgsz, device="cpu", verbose=False)
        self.skip = max(0, int(skip))
        self.drop_backlog = bool(drop_backlog)

        # ── window state ───────────────────────────────────────────────────
        self.window_deadline = 0.0
        self.window_uid = None
        self.result_sent = False
        self.lock = threading.Lock()

        # ── view ───────────────────────────────────────────────────────────
        cv2.namedWindow("Processing", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("Processing", window_w, window_h)

        # ── start control thread ───────────────────────────────────────────
        threading.Thread(target=self._ctrl_loop, daemon=True).start()

        print("[CL] connected: stream @{}, control @{}".format(stream_port, ctrl_port))

    # ---------- Control ----------
    def _ctrl_loop(self):
        buf = b""
        while True:
            data = self.c.recv(4096)
            if not data:
                print("[CL] control disconnected"); break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line.decode())
                except Exception:
                    continue
                if msg.get("cmd") == "start":
                    window_s = int(msg.get("window_s", 5))
                    uid = str(msg.get("uid"))
                    with self.lock:
                        self.window_deadline = time.time() + window_s
                        self.window_uid = uid
                        self.result_sent = False
                    print(f"[CL] START uid={uid} window={window_s}s")

    def _send_det_result_once(self, ok: bool):
        with self.lock:
            if self.result_sent:
                return
            self.result_sent = True
        try:
            payload = json.dumps({"cmd":"det_result","ok": bool(ok)}) + "\n"
            self.c.sendall(payload.encode())
            print(f"[CL] det_result sent: {ok}")
        except Exception as e:
            print("[CL] det_result send error:", e)

    # ---------- Stream ----------
    def _recv_exact(self, n: int):
        while len(self.buf) < n:
            pkt = self.s.recv(self.CHUNK)
            if not pkt:
                return None
            self.buf += pkt
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _drop_backlog_frames(self):
        if not self.drop_backlog:
            return
        while True:
            if len(self.buf) < self.HDR: break
            next_sz = struct.unpack(">I", self.buf[:self.HDR])[0]
            if len(self.buf) >= self.HDR + next_sz:
                self.buf = self.buf[self.HDR + next_sz :]
                continue
            break

    def _detect_helmet(self, frame):
        res = self.model(frame, **self.infer_kwargs)[0]
        names = res.names if hasattr(res, "names") and isinstance(res.names, dict) else self.model.names
        found = False
        if res.boxes is not None:
            xyxy = res.boxes.xyxy.cpu().numpy()
            conf = res.boxes.conf.cpu().numpy()
            cls  = res.boxes.cls.cpu().numpy().astype(int)
            for (x1, y1, x2, y2), sc, c in zip(xyxy, conf, cls):
                name = names.get(c, str(c)).lower()
                is_helmet = ("helmet" in name) or ("hardhat" in name)
                color = (0,255,0) if is_helmet else (0,0,255)
                x1, y1, x2, y2 = map(int, (x1, y1, x2, y2))
                cv2.rectangle(frame, (x1,y1), (x2,y2), color, 2)
                cv2.putText(frame, f"{name}:{sc:.2f}", (x1, max(12, y1-5)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
                if is_helmet:
                    found = True
        return found

    def run(self):
        fps_hist, i = [], 0
        try:
            while True:
                t0 = time.perf_counter()

                # header
                hdr = self._recv_exact(self.HDR)
                if hdr is None: break
                n = struct.unpack(">I", hdr)[0]
                if not (1_000 <= n <= 2_000_000):
                    self.buf = b""; continue
                # payload
                jpg = self._recv_exact(n)
                if jpg is None: break
                if not (jpg[:2] == b"\xff\xd8" and jpg[-2:] == b"\xff\xd9"):
                    self.buf = b""; continue

                # backlog drop (low latency)
                self._drop_backlog_frames()

                frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                if frame is None: continue

                # --- window check ---
                with self.lock:
                    deadline = self.window_deadline
                    result_sent = self.result_sent
                now = time.time()
                window_on = (deadline > now)

                # 추론은 윈도우 중에만 수행(바깥에선 디스플레이만)
                helmet_ok = False
                if window_on and (self.skip == 0 or i % (self.skip + 1) == 0):
                    helmet_ok = self._detect_helmet(frame)
                    if helmet_ok and not result_sent:
                        self._send_det_result_once(True)
                        # 윈도우 즉시 종료(선택): 다음 줄 주석 해제 시 즉시 끝냄
                        # with self.lock: self.window_deadline = 0.0

                # 타임아웃 처리
                if (not result_sent) and (deadline > 0) and (now > deadline):
                    self._send_det_result_once(False)
                    with self.lock: self.window_deadline = 0.0

                # HUD
                fps = 1.0 / max(1e-3, time.perf_counter() - t0)
                fps_hist.append(fps); fps_hist = fps_hist[-30:]
                cv2.putText(frame, f"FPS:{sum(fps_hist)/len(fps_hist):.1f} | CPU",
                            (10,30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2)
                if window_on:
                    left = max(0.0, deadline - now)
                    cv2.putText(frame, f"WINDOW {left:.1f}s",
                                (10,60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,255), 2)
                if helmet_ok:
                    cv2.putText(frame, "HELMET: OK", (10,90),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,255,0), 2)

                cv2.imshow("Processing", frame)
                i += 1
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
        finally:
            try: cv2.destroyAllWindows()
            except: pass
            try: self.s.close()
            except: pass
            try: self.c.close()
            except: pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pi-ip", required=True)
    ap.add_argument("--stream-port", type=int, default=9999)
    ap.add_argument("--ctrl-port",   type=int, default=9998)
    ap.add_argument("--model", default="best.pt")
    ap.add_argument("--conf", type=float, default=0.5)
    ap.add_argument("--iou",  type=float, default=0.45)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--skip", type=int, default=0)
    ap.add_argument("--no-drop", action="store_true")
    ap.add_argument("--recvbuf-kb", type=int, default=256)
    args = ap.parse_args()

    cli = GateClient(
        pi_ip=args.pi_ip,
        stream_port=args.stream_port,
        ctrl_port=args.ctrl_port,
        model_path=args.model,
        conf=args.conf, iou=args.iou, imgsz=args.imgsz,
        skip=args.skip, drop_backlog=not args.no_drop, recvbuf_kb=args.recvbuf_kb
    )
    cli.run()

if __name__ == "__main__":
    main()
