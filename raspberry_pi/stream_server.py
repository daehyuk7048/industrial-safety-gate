#!/usr/bin/env python3
"""
Raspberry Pi → Laptop TCP JPEG Streamer (Picamera2)
- 4바이트 Big-endian 헤더(>I) + JPEG payload
- 연속 메모리 보장(np.ascontiguousarray)로 줄무늬/깨짐 방지
- TCP_NODELAY, FPS 제한, 단일/다중 접속 처리
- 색공간 토글: 센서가 RGB로 나오면 --colorspace rgb (RGB→BGR 변환 후 인코딩)
"""

import socket
import struct
import cv2
import time
import sys
import numpy as np
from picamera2 import Picamera2
import argparse
import signal

class StreamServer:
    def __init__(
        self,
        host="0.0.0.0",
        port=9999,
        width=640,
        height=480,
        fps=15,
        jpeg_q=75,
        colorspace="auto",        # "auto"|"rgb"|"bgr"
        debug=False,
    ):
        self.addr = (host, port)
        self.width = int(width)
        self.height = int(height)
        self.target_fps = max(1, int(fps))
        self.frame_interval = 1.0 / self.target_fps
        self.jpeg_q = int(jpeg_q)
        self.colorspace = colorspace.lower()
        self.debug = debug

        self.picam2 = None
        self.sock = None
        self.stop_flag = False

    # ---------- Camera ----------
    def init_camera(self):
        self.picam2 = Picamera2()
        cfg = self.picam2.create_preview_configuration(
            main={"size": (self.width, self.height), "format": "RGB888"},
            buffer_count=1,
            queue=False,
        )
        self.picam2.configure(cfg)
        self.picam2.start()
        time.sleep(0.4)
        return True

    # ---------- Networking ----------
    def init_socket(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(self.addr)
        self.sock.listen(1)
        print(f"[WAIT] Listening on {self.addr[0]}:{self.addr[1]} ...")

    def accept_one(self):
        conn, cli = self.sock.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f"[CONNECTED] {cli}")
        return conn, cli

    # ---------- Streaming ----------
    def stream_loop(self, conn):
        frame_count = 0
        first_saved = False
        try:
            while not self.stop_flag:
                t0 = time.perf_counter()

                frame = self.picam2.capture_array()  # RGB888 by config


                # 줄무늬/깨짐 방지: 연속 메모리 보장
                frame = np.ascontiguousarray(frame)

                ok, jpg = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_q]
                )
                if not ok:
                    continue
                data = jpg.tobytes()

                # (선택) 첫 프레임 디버그 저장
                if self.debug and not first_saved:
                    with open("tx_sample.jpg", "wb") as f:
                        f.write(data)
                    print(f"[DEBUG] saved tx_sample.jpg ({len(data)} bytes)")
                    # 채널 평균도 출력(변환 전/후 확인용)
                    try:
                        b, g, r = [float(np.mean(frame[:, :, i])) for i in range(3)]
                        print(f"[DEBUG] mean BGR = {b:.1f}, {g:.1f}, {r:.1f}")
                    except Exception:
                        pass
                    first_saved = True

                # 헤더(4B, big-endian) + 페이로드
                conn.sendall(struct.pack(">I", len(data)))
                conn.sendall(data)

                frame_count += 1
                if frame_count % 100 == 0:
                    print(f"[TX] frames={frame_count}, size≈{len(data)}B, q={self.jpeg_q}")

                # FPS 제한
                dt = time.perf_counter() - t0
                if dt < self.frame_interval:
                    time.sleep(self.frame_interval - dt)

        except (BrokenPipeError, ConnectionResetError):
            print("[INFO] client disconnected")
        except KeyboardInterrupt:
            self.stop_flag = True
            print("\n[STOP] keyboard interrupt")
        finally:
            try:
                conn.close()
            except Exception:
                pass
            print(f"[END] sent {frame_count} frames")

    # ---------- Run ----------
    def run(self):
        # Ctrl+C 핸들러
        signal.signal(signal.SIGINT, lambda sig, frm: setattr(self, "stop_flag", True))

        self.init_camera()
        self.init_socket()

        while not self.stop_flag:
            try:
                conn, _ = self.accept_one()
                self.stream_loop(conn)
            except KeyboardInterrupt:
                self.stop_flag = True
                break
            except Exception as e:
                print(f"[ERROR] {e}")
                time.sleep(0.5)

        try:
            self.picam2.stop()
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        print("[EXIT] server closed")

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Picamera2 TCP JPEG Stream Server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--jpeg-q", type=int, default=75, help="JPEG quality 1~100")
    ap.add_argument(
        "--colorspace",
        default="auto",
        choices=["auto", "rgb", "bgr"],
        help="센서 버퍼 색공간: 'rgb'이면 RGB→BGR 변환 후 JPEG 인코딩",
    )
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    srv = StreamServer(
        host=args.host,
        port=args.port,
        width=args.width,
        height=args.height,
        fps=args.fps,
        jpeg_q=args.jpeg_q,
        colorspace=args.colorspace,
        debug=args.debug,
    )
    srv.run()

if __name__ == "__main__":
    main()
