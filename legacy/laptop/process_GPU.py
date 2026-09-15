#!/usr/bin/env python3
import socket
import struct
import time
import cv2
import numpy as np
from pathlib import Path
from openvino import Core

class OVClient:
    def __init__(self, server_ip, port=9999, model_xml="best.xml",
                 device="GPU", conf_thres=0.5, iou_thres=0.45):
        
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.s.connect((server_ip, port))
        self.buf = b""
        self.hdr = struct.calcsize(">I")
        self.RECV_CHUNK = 65536
        
        xml = Path(model_xml)
        if not xml.exists() or not xml.with_suffix(".bin").exists():
            raise FileNotFoundError(f"Model files absent: {xml}")
        
        ie = Core()
        dev = device.upper()
        if dev == "MAX":
            dev = "MULTI:GPU,CPU"
        
        model = ie.read_model(str(xml))
        compiled = ie.compile_model(model, dev)
        self.req = compiled.create_infer_request()
        self.inp = compiled.input(0)
        _, _, self.in_h, self.in_w = self.inp.shape
        
        self.conf_thres = conf_thres
        self.iou_thres = iou_thres
        self.names = ["helmet", "no-helmet"]
        
        print(f"[INIT] {dev} | {self.in_w}x{self.in_h}")
    
    def _recv_exact(self, n):
        while len(self.buf) < n:
            pkt = self.s.recv(self.RECV_CHUNK)
            if not pkt:
                return None
            self.buf += pkt
        out, self.buf = self.buf[:n], self.buf[n:]
        return out
    
    def _letterbox(self, img):
        h, w = img.shape[:2]
        r = min(self.in_h/h, self.in_w/w)
        nh, nw = int(h*r), int(w*r)
        pad_w = (self.in_w - nw) // 2
        pad_h = (self.in_h - nh) // 2
        
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        img = cv2.copyMakeBorder(img, pad_h, self.in_h-nh-pad_h,
                                pad_w, self.in_w-nw-pad_w,
                                cv2.BORDER_CONSTANT, value=(114,114,114))
        return img, r, (pad_w, pad_h)
    
    def _postprocess(self, output, frame, r, pad):
        pred = output[0]
        
        # 데이터 형상 진단
        print(f"[DEBUG] Output shape: {pred.shape}")
        print(f"[DEBUG] Value range: [{pred.min():.3f}, {pred.max():.3f}]")
        
        # 차원 정규화
        if pred.ndim == 3:
            if pred.shape[1] == 84:
                pred = pred.squeeze(0).T  # (84, 8400) -> (8400, 84)
            else:
                pred = pred.squeeze(0)
        
        # 샘플 데이터 출력
        print(f"[DEBUG] Sample predictions (first 3):")
        for i in range(min(3, len(pred))):
            print(f"  Box {i}: x={pred[i,0]:.2f} y={pred[i,1]:.2f} w={pred[i,2]:.2f} h={pred[i,3]:.2f} obj={pred[i,4]:.3f}")
        
        # 객체성 필터링
        mask = pred[:, 4] > self.conf_thres
        pred = pred[mask]
        print(f"[DEBUG] Filtered: {len(pred)} boxes (threshold={self.conf_thres})")
        
        if len(pred) == 0:
            return []
        
        boxes = pred[:, :4].copy()
        scores = pred[:, 4].copy()
        class_ids = pred[:, 5:].argmax(axis=1)
        
        # 좌표 스케일 진단
        print(f"[DEBUG] Box coordinate range: [{boxes.min():.2f}, {boxes.max():.2f}]")
        
        # 정규화 검출 및 복원
        if boxes.max() <= 1.0:
            print(f"[DEBUG] Normalized coords detected, scaling to {self.in_w}x{self.in_h}")
            boxes[:, [0, 2]] *= self.in_w
            boxes[:, [1, 3]] *= self.in_h
        
        # XYWH → XYXY 변환
        x_c = boxes[:, 0]
        y_c = boxes[:, 1]
        w = boxes[:, 2]
        h_box = boxes[:, 3]
        
        x1 = x_c - w/2
        y1 = y_c - h_box/2
        x2 = x_c + w/2
        y2 = y_c + h_box/2
        
        print(f"[DEBUG] After XYXY conversion: x1={x1[0]:.2f} y1={y1[0]:.2f} x2={x2[0]:.2f} y2={y2[0]:.2f}")
        
        # Letterbox 역변환
        x1 = (x1 - pad[0]) / r
        y1 = (y1 - pad[1]) / r
        x2 = (x2 - pad[0]) / r
        y2 = (y2 - pad[1]) / r
        
        print(f"[DEBUG] After letterbox inverse: x1={x1[0]:.2f} y1={y1[0]:.2f} x2={x2[0]:.2f} y2={y2[0]:.2f}")
        print(f"[DEBUG] Frame size: {frame.shape[1]}x{frame.shape[0]}, r={r:.3f}, pad={pad}")
        
        boxes = np.stack([x1, y1, x2, y2], axis=1)
        
        # 경계 클리핑
        h, w = frame.shape[:2]
        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, w)
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, h)
        
        # NMS
        indices = cv2.dnn.NMSBoxes(
            boxes.tolist(),
            scores.tolist(),
            self.conf_thres,
            self.iou_thres
        )
        
        print(f"[DEBUG] NMS: {len(indices) if indices else 0} boxes retained")
        
        if len(indices) == 0:
            return []
        
        indices = indices.flatten()
        results = []
        for i in indices:
            results.append({
                'box': boxes[i],
                'score': scores[i],
                'class': class_ids[i]
            })
        
        return results


    def run(self):
        fps_hist = []
        frame_count = 0
        
        while True:
            t0 = time.perf_counter()
            
            h = self._recv_exact(self.hdr)
            if h is None:
                break
            n = struct.unpack(">I", h)[0]
            
            if not (1000 <= n <= 2000000):
                self.buf = b""
                continue
            
            jpg = self._recv_exact(n)
            if jpg is None:
                break
            
            frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            
            img, r, pad = self._letterbox(frame)
            blob = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            blob = blob.transpose(2,0,1)[None].astype(np.float32) / 255.0
            
            self.req.infer({self.inp.any_name: blob})
            output = self.req.get_output_tensor(0).data
            
            detections = self._postprocess(output, frame, r, pad)
            
            for det in detections:
                x1, y1, x2, y2 = det['box'].astype(int)
                score = det['score']
                cls = det['class']
                
                name = self.names[cls] if cls < len(self.names) else str(cls)
                color = (0, 255, 0) if cls == 0 else (0, 0, 255)
                
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(frame, f"{name}:{score:.2f}", (x1, max(15, y1-5)),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
            
            fps = 1.0 / max(1e-3, time.perf_counter() - t0)
            fps_hist.append(fps)
            fps_hist = fps_hist[-30:]
            avg_fps = sum(fps_hist) / len(fps_hist)
            
            cv2.putText(frame, f"FPS:{avg_fps:.1f} | OpenVINO/GPU", (10, 30),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            
            cv2.imshow("Processing", frame)
            frame_count += 1
            
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
        
        cv2.destroyAllWindows()
        self.s.close()

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("server_ip")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--model", required=True)
    ap.add_argument("--device", default="GPU")
    ap.add_argument("--conf", type=float, default=0.5)
    ap.add_argument("--iou", type=float, default=0.45)
    args = ap.parse_args()
    
    OVClient(args.server_ip, args.port, args.model,
             args.device, args.conf, args.iou).run()