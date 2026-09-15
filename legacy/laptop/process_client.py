#!/usr/bin/env python3
import socket, struct, time, cv2, numpy as np, argparse
from ultralytics import YOLO

class ProcessClientCPU:
    def __init__(
        self,
        server_ip: str,
        port: int = 9999,
        model_path: str = "best.pt",
        conf: float = 0.5,
        iou: float = 0.45,
        imgsz: int = 640,
        skip: int = 0,
        drop_backlog: bool = True,
        recvbuf_kb: int = 256,      # 소켓 수신버퍼(작게=지연↓)
        window_w: int = 960,
        window_h: int = 540,
    ):
        # ── socket ──────────────────────────────────────────────────────────
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if recvbuf_kb > 0:
            self.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, recvbuf_kb * 1024)
        self.s.connect((server_ip, port))

        self.buf = b""
        self.HDR = struct.calcsize(">I")
        self.CHUNK = 65536

        # ── YOLO (CPU 전용) ────────────────────────────────────────────────
        self.model = YOLO(model_path)
        self.infer_kwargs = dict(conf=conf, iou=iou, imgsz=imgsz, device="cpu", verbose=False)
        self.skip = max(0, int(skip))
        self.drop_backlog = bool(drop_backlog)

        # OpenCV 최적화
        try:
            cv2.setUseOptimized(True)
        except Exception:
            pass

        # 보기 편의
        try:
            cv2.namedWindow("Processing", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("Processing", window_w, window_h)
        except Exception:
            pass

    # 정확히 n바이트 수신
    def _recv_exact(self, n: int):
        while len(self.buf) < n:
            pkt = self.s.recv(self.CHUNK)
            if not pkt:
                return None
            self.buf += pkt
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    # 백로그 프레임 한꺼번에 버리고 최신 프레임만 남기기(저지연)
    def _drop_backlog_frames(self):
        if not self.drop_backlog:
            return
        while True:
            if len(self.buf) < self.HDR:
                break
            next_sz = struct.unpack(">I", self.buf[:self.HDR])[0]
            if len(self.buf) >= self.HDR + next_sz:
                # 이 프레임은 버리고 다음으로 이동
                self.buf = self.buf[self.HDR + next_sz :]
                continue
            break

    def run(self):
        fps_hist, i = [], 0
        last = None
        try:
            while True:
                t0 = time.perf_counter()

                # 4B 길이
                hdr = self._recv_exact(self.HDR)
                if hdr is None:
                    break
                n = struct.unpack(">I", hdr)[0]
                if not (1_000 <= n <= 2_000_000):   # 비정상 방어
                    self.buf = b""
                    continue

                # JPEG payload
                jpg = self._recv_exact(n)
                if jpg is None:
                    break
                # 안전: SOI/EOI 검사
                if not (jpg[:2] == b"\xff\xd8" and jpg[-2:] == b"\xff\xd9"):
                    self.buf = b""
                    continue

                # 백로그 프레임 드롭(저지연)
                self._drop_backlog_frames()

                # 디코드(BGR)
                frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    continue

                # 추론(스킵 지원)
                if self.skip == 0 or i % (self.skip + 1) == 0:
                    res = self.model(frame, **self.infer_kwargs)[0]
                    last = res
                else:
                    res = last

                # 렌더링
                if res is not None and res.boxes is not None:
                    names = self.model.names
                    xyxy = res.boxes.xyxy.cpu().numpy()
                    conf = res.boxes.conf.cpu().numpy()
                    cls  = res.boxes.cls.cpu().numpy().astype(int)

                    for (x1, y1, x2, y2), sc, c in zip(xyxy, conf, cls):
                        x1, y1, x2, y2 = map(int, (x1, y1, x2, y2))
                        name = names.get(c, str(c))
                        color = (0, 255, 0) if ("helmet" in name.lower() or "hardhat" in name.lower()) else (0, 0, 255)
                        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                        cv2.putText(frame, f"{name}:{sc:.2f}", (x1, max(12, y1 - 5)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

                # FPS
                fps = 1.0 / max(1e-3, time.perf_counter() - t0)
                fps_hist.append(fps)
                fps_hist = fps_hist[-30:]
                cv2.putText(frame, f"FPS:{sum(fps_hist)/len(fps_hist):.1f} | CPU",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

                cv2.imshow("Processing", frame)
                i += 1
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
        finally:
            try:
                cv2.destroyAllWindows()
            finally:
                self.s.close()

# ── CLI ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("server_ip")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--model", default="best.pt")
    ap.add_argument("--conf", type=float, default=0.5)
    ap.add_argument("--iou",  type=float, default=0.45)
    ap.add_argument("--imgsz", type=int, default=640, help="추론 입력 크기(낮추면 CPU 더 빨라짐, 예: 512/416)")
    ap.add_argument("--skip", type=int, default=0, help="추론 스킵(0=모두, 1=2장중1장 등)")
    ap.add_argument("--no-drop", action="store_true", help="프레임 백로그 드롭 비활성화")
    ap.add_argument("--recvbuf-kb", type=int, default=256, help="소켓 수신버퍼 KB(작게=지연↓)")
    args = ap.parse_args()

    ProcessClientCPU(
        server_ip=args.server_ip,
        port=args.port,
        model_path=args.model,
        conf=args.conf,
        iou=args.iou,
        imgsz=args.imgsz,
        skip=args.skip,
        drop_backlog=not args.no_drop,
        recvbuf_kb=args.recvbuf_kb,
    ).run()
