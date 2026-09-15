#!/usr/bin/env python3
"""
라즈베리파이 5 - 안전모 탐지 시스템
하드웨어: RPi5 + Camera Module V3
모델: YOLOv8 커스텀 학습 모델
"""

import cv2
import numpy as np
from ultralytics import YOLO
from picamera2 import Picamera2
import time
import sys
from pathlib import Path

class HelmetDetector:
    def __init__(self, model_path, conf_threshold=0.5, iou_threshold=0.45):
        """
        매개변수:
        - model_path: .pt 파일 경로
        - conf_threshold: 신뢰도 임계값
        - iou_threshold: NMS IOU 임계값
        """
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        
        # YOLOv8 모델 로드
        try:
            self.model = YOLO(model_path)
            # YOLOv8 추론 설정
            self.model.overrides['conf'] = conf_threshold
            self.model.overrides['iou'] = iou_threshold
            self.model.overrides['verbose'] = False  # 출력 최소화
            
            print(f"모델 로드 완료: {model_path}")
            print(f"모델 파라미터: {sum(p.numel() for p in self.model.model.parameters()):,}")
            
        except Exception as e:
            print(f"모델 로드 실패: {e}")
            sys.exit(1)
            
    def initialize_camera(self, width=640, height=480):
        """
        Picamera2 초기화
        RPi Camera V3 최적 설정
        """
        try:
            self.picam2 = Picamera2()
            
            # 카메라 구성
            config = self.picam2.create_preview_configuration(
                main={"size": (width, height), "format": "RGB888"},
                buffer_count=2  # 버퍼 최소화로 지연 감소
            )
            self.picam2.configure(config)
            self.picam2.start()
            
            # 안정화 대기
            time.sleep(0.5)
            
            print(f"카메라 초기화 완료: {width}x{height}")
            return True
            
        except Exception as e:
            print(f"카메라 초기화 실패: {e}")
            return False
            
    def detect_frame(self, frame):
        """
        단일 프레임 탐지 - YOLOv8 방식
        """
        # YOLOv8 추론
        results = self.model.predict(
            frame,
            conf=self.conf_threshold,
            iou=self.iou_threshold,
            verbose=False
        )
        
        return results[0] if results else None
        
    def draw_detections(self, frame, result):
        """
        탐지 결과 시각화 - YOLOv8 형식
        """
        if result is None or result.boxes is None:
            return frame
            
        boxes = result.boxes
        
        for box in boxes:
            # 좌표 추출
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
            conf = box.conf[0].cpu().numpy()
            cls_id = int(box.cls[0].cpu().numpy())
            
            # 클래스 이름 획득
            cls_name = result.names[cls_id] if hasattr(result, 'names') else str(cls_id)
            
            # 바운딩 박스
            color = (0, 255, 0) if 'helmet' in cls_name.lower() else (0, 0, 255)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            
            # 라벨
            label = f"{cls_name}: {conf:.2f}"
            label_size, _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(frame, (x1, y1-20), (x1+label_size[0], y1), color, -1)
            cv2.putText(frame, label, (x1, y1-5), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            
        return frame
        
    def run_detection(self, display=True, save_video=False, output_path='output.mp4'):
        """
        실시간 탐지 실행
        """
        if not self.initialize_camera():
            return
            
        # 비디오 저장 설정
        fourcc = None
        out = None
        if save_video:
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            out = cv2.VideoWriter(output_path, fourcc, 20.0, (640, 480))
            
        # FPS 계산 변수
        fps_counter = 0
        start_time = time.time()
        fps = 0
        
        print("탐지 시작 (종료: 'q' 키)")
        
        try:
            while True:
                # 프레임 캡처
                frame = self.picam2.capture_array()
                
                # BGR 변환 (OpenCV 호환)
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                
                # 탐지 수행
                result = self.detect_frame(frame)
                
                # 시각화
                if result and result.boxes is not None:
                    frame_bgr = self.draw_detections(frame_bgr, result)
                    detection_count = len(result.boxes)
                else:
                    detection_count = 0
                    
                # FPS 계산
                fps_counter += 1
                if fps_counter % 30 == 0:
                    end_time = time.time()
                    fps = 30 / (end_time - start_time)
                    start_time = time.time()
                    
                # FPS 표시
                cv2.putText(frame_bgr, f"FPS: {fps:.1f}", (10, 30),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                
                # 탐지 수 표시
                cv2.putText(frame_bgr, f"Objects: {detection_count}", (10, 60),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                
                # 화면 표시
                if display:
                    cv2.imshow('Helmet Detection - RPi5', frame_bgr)
                    
                # 비디오 저장
                if save_video and out:
                    out.write(frame_bgr)
                    
                # 종료 조건
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
                    
        except KeyboardInterrupt:
            print("\n사용자 중단")
            
        finally:
            # 정리
            self.picam2.stop()
            cv2.destroyAllWindows()
            if out:
                out.release()
                print(f"비디오 저장: {output_path}")
                
            print("탐지 종료")
            
    def test_single_image(self, image_path):
        """
        단일 이미지 테스트 - YOLOv8 방식
        """
        image = cv2.imread(image_path)
        if image is None:
            print(f"이미지 로드 실패: {image_path}")
            return
            
        result = self.detect_frame(image)
        
        if result and result.boxes is not None:
            image = self.draw_detections(image, result)
            detection_count = len(result.boxes)
            print(f"탐지 결과: {detection_count} 객체")
            
            # 상세 정보 출력
            for i, box in enumerate(result.boxes):
                cls_id = int(box.cls[0].cpu().numpy())
                cls_name = result.names[cls_id] if hasattr(result, 'names') else str(cls_id)
                conf = box.conf[0].cpu().numpy()
                print(f"  - {cls_name}: {conf:.2f}")
        else:
            print("탐지된 객체 없음")
            
        # 결과 표시
        cv2.imshow('Detection Result', image)
        cv2.waitKey(0)
        cv2.destroyAllWindows()
        
        # 결과 저장
        output_name = Path(image_path).stem + '_detected.jpg'
        cv2.imwrite(output_name, image)
        print(f"결과 저장: {output_name}")
        
    def benchmark(self, iterations=100):
        """
        모델 성능 벤치마크
        """
        # 더미 이미지 생성
        dummy_image = np.random.randint(0, 255, (640, 480, 3), dtype=np.uint8)
        
        # 워밍업
        for _ in range(10):
            _ = self.model.predict(dummy_image, verbose=False)
        
        # 벤치마크
        times = []
        for _ in range(iterations):
            start = time.perf_counter()
            _ = self.model.predict(dummy_image, verbose=False)
            times.append(time.perf_counter() - start)
        
        # 통계 계산
        mean_time = np.mean(times) * 1000  # ms
        std_time = np.std(times) * 1000
        min_time = np.min(times) * 1000
        max_time = np.max(times) * 1000
        
        print(f"\n벤치마크 결과 ({iterations}회 반복):")
        print(f"  평균: {mean_time:.2f} ms")
        print(f"  표준편차: {std_time:.2f} ms")
        print(f"  최소: {min_time:.2f} ms")
        print(f"  최대: {max_time:.2f} ms")
        print(f"  추정 FPS: {1000/mean_time:.1f}")


def main():
    """
    메인 실행 함수
    """
    import argparse
    
    parser = argparse.ArgumentParser(description='RPi5 안전모 탐지 시스템 - YOLOv8')
    parser.add_argument('--model', type=str, required=True,
                       help='YOLOv8 모델 경로 (.pt)')
    parser.add_argument('--conf', type=float, default=0.5,
                       help='신뢰도 임계값 (기본: 0.5)')
    parser.add_argument('--iou', type=float, default=0.45,
                       help='IOU 임계값 (기본: 0.45)')
    parser.add_argument('--no-display', action='store_true',
                       help='화면 표시 비활성화')
    parser.add_argument('--save-video', action='store_true',
                       help='비디오 저장')
    parser.add_argument('--output', type=str, default='detection_output.mp4',
                       help='출력 비디오 경로')
    parser.add_argument('--test-image', type=str,
                       help='단일 이미지 테스트')
    parser.add_argument('--benchmark', action='store_true',
                       help='성능 벤치마크 실행')
    
    args = parser.parse_args()
    
    # 탐지기 초기화
    detector = HelmetDetector(
        model_path=args.model,
        conf_threshold=args.conf,
        iou_threshold=args.iou
    )
    
    # 실행 모드
    if args.benchmark:
        # 벤치마크 모드
        detector.benchmark()
    elif args.test_image:
        # 이미지 테스트 모드
        detector.test_single_image(args.test_image)
    else:
        # 실시간 탐지 모드
        detector.run_detection(
            display=not args.no_display,
            save_video=args.save_video,
            output_path=args.output
        )


if __name__ == "__main__":
    main()