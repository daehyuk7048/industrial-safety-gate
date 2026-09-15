#!/usr/bin/env python3
"""
라즈베리파이 5 - IMX500 안전모 탐지 시스템
하드웨어: RPi5 + IMX500 AI Camera
모델: YOLOv8 커스텀 학습 모델
"""

import cv2
import numpy as np
from picamera2 import Picamera2
from ultralytics import YOLO
import time
import sys
from pathlib import Path
import argparse

class HelmetDetector:
    def __init__(self, model_path, conf_threshold=0.5, iou_threshold=0.45):
        """
        매개변수:
        - model_path: YOLOv8 .pt 파일 경로
        - conf_threshold: 신뢰도 임계값
        - iou_threshold: NMS IOU 임계값
        """
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.picam2 = None
        
        # YOLOv8 모델 로드
        try:
            self.model = YOLO(model_path)
            print(f"모델 로드 완료: {model_path}")
            print(f"모델 클래스: {self.model.names}")
        except Exception as e:
            print(f"모델 로드 실패: {e}")
            sys.exit(1)
            
    def initialize_camera(self, width=2028, height=1520):
        """
        IMX500 카메라 초기화
        """
        try:
            self.picam2 = Picamera2()
            
            # IMX500 지원 모드 확인
            camera_info = self.picam2.camera_properties
            print(f"카메라 모델: {camera_info.get('Model', 'Unknown')}")
            
            # IMX500 최적 구성
            config = self.picam2.create_preview_configuration(
                main={
                    "size": (width, height),
                    "format": "RGB888"
                },
                buffer_count=4,  # IMX500용 버퍼 증가
                queue=True
            )
            
            self.picam2.configure(config)
            
            # 카메라 컨트롤 설정
            self.picam2.set_controls({
                "ExposureTime": 20000,  # 20ms
                "AnalogueGain": 2.0,
                "AeEnable": True,
                "AwbEnable": True
            })
            
            self.picam2.start()
            
            # 안정화 대기
            time.sleep(1.0)
            
            print(f"카메라 초기화 완료: {width}x{height}")
            return True
            
        except Exception as e:
            print(f"카메라 초기화 실패: {e}")
            print("디버그 정보:")
            print(f"  - 카메라 감지: {Picamera2.global_camera_info()}")
            return False
            
    def detect_frame(self, frame):
        """
        YOLOv8 프레임 탐지
        """
        # YOLOv8 추론
        results = self.model(
            frame,
            conf=self.conf_threshold,
            iou=self.iou_threshold,
            verbose=False
        )
        
        return results[0]
        
    def draw_detections(self, frame, result):
        """
        YOLOv8 탐지 결과 시각화
        """
        if result.boxes is None:
            return frame
            
        boxes = result.boxes
        for box in boxes:
            # 좌표 추출
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
            conf = box.conf[0].cpu().numpy()
            cls = int(box.cls[0].cpu().numpy())
            
            # 클래스명 가져오기
            class_name = self.model.names[cls]
            
            # 색상 결정
            color = (0, 255, 0) if 'helmet' in class_name.lower() else (0, 0, 255)
            
            # 바운딩 박스
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            
            # 라벨
            label = f"{class_name}: {conf:.2f}"
            label_size, _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            
            # 라벨 배경
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
        out = None
        if save_video:
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            out = cv2.VideoWriter(output_path, fourcc, 15.0, (2028, 1520))
            
        # FPS 계산 변수
        fps_list = []
        frame_count = 0
        
        print("탐지 시작 (종료: 'q' 키)")
        print("-" * 50)
        
        try:
            while True:
                start_time = time.time()
                
                # 프레임 캡처
                frame = self.picam2.capture_array()
                
                # BGR 변환 (OpenCV 호환)
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                
                # 탐지 수행
                result = self.detect_frame(frame)
                
                # 시각화
                frame_bgr = self.draw_detections(frame_bgr, result)
                
                # FPS 계산
                end_time = time.time()
                fps = 1 / (end_time - start_time)
                fps_list.append(fps)
                if len(fps_list) > 30:
                    fps_list.pop(0)
                avg_fps = sum(fps_list) / len(fps_list)
                
                # 정보 오버레이
                detection_count = len(result.boxes) if result.boxes is not None else 0
                
                # 배경 박스
                cv2.rectangle(frame_bgr, (10, 10), (250, 90), (0, 0, 0), -1)
                cv2.rectangle(frame_bgr, (10, 10), (250, 90), (0, 255, 0), 2)
                
                # 텍스트
                cv2.putText(frame_bgr, f"FPS: {avg_fps:.1f}", (20, 35),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.putText(frame_bgr, f"Objects: {detection_count}", (20, 60),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.putText(frame_bgr, f"Frame: {frame_count}", (20, 85),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                
                # 화면 표시
                if display:
                    # 디스플레이용 크기 조정 (선택사항)
                    display_frame = cv2.resize(frame_bgr, (1014, 760))
                    cv2.imshow('YOLOv8 Helmet Detection - IMX500', display_frame)
                    
                # 비디오 저장
                if save_video and out:
                    out.write(frame_bgr)
                    
                frame_count += 1
                
                # 종료 조건
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
                    
                # 주기적 상태 출력
                if frame_count % 100 == 0:
                    print(f"처리 프레임: {frame_count}, 평균 FPS: {avg_fps:.1f}")
                    
        except KeyboardInterrupt:
            print("\n사용자 중단")
            
        except Exception as e:
            print(f"실행 중 오류: {e}")
            
        finally:
            # 정리
            if self.picam2:
                self.picam2.stop()
            cv2.destroyAllWindows()
            if out:
                out.release()
                print(f"비디오 저장: {output_path}")
                
            print(f"총 처리 프레임: {frame_count}")
            if fps_list:
                print(f"평균 FPS: {sum(fps_list)/len(fps_list):.1f}")
            print("탐지 종료")
            
    def test_single_image(self, image_path):
        """
        단일 이미지 테스트
        """
        image = cv2.imread(image_path)
        if image is None:
            print(f"이미지 로드 실패: {image_path}")
            return
            
        # 탐지 수행
        result = self.detect_frame(image)
        
        # 결과 출력
        if result.boxes is not None:
            print(f"탐지 결과: {len(result.boxes)} 객체")
            for box in result.boxes:
                cls = int(box.cls[0].cpu().numpy())
                conf = box.conf[0].cpu().numpy()
                print(f"  - {self.model.names[cls]}: {conf:.2f}")
        else:
            print("탐지된 객체 없음")
            
        # 시각화
        image = self.draw_detections(image, result)
        
        # 결과 표시
        cv2.imshow('Detection Result', image)
        cv2.waitKey(0)
        cv2.destroyAllWindows()
        
        # 결과 저장
        output_name = Path(image_path).stem + '_detected.jpg'
        cv2.imwrite(output_name, image)
        print(f"결과 저장: {output_name}")

    def benchmark(self, duration=30):
        """
        성능 벤치마크
        """
        if not self.initialize_camera():
            return
            
        print(f"벤치마크 시작 ({duration}초)")
        
        frame_times = []
        detection_times = []
        start = time.time()
        
        while time.time() - start < duration:
            t0 = time.time()
            
            # 프레임 캡처
            frame = self.picam2.capture_array()
            t1 = time.time()
            
            # 탐지
            result = self.detect_frame(frame)
            t2 = time.time()
            
            frame_times.append(t1 - t0)
            detection_times.append(t2 - t1)
            
        self.picam2.stop()
        
        # 통계
        print("\n=== 벤치마크 결과 ===")
        print(f"총 프레임: {len(frame_times)}")
        print(f"평균 캡처 시간: {np.mean(frame_times)*1000:.2f}ms")
        print(f"평균 탐지 시간: {np.mean(detection_times)*1000:.2f}ms")
        print(f"평균 총 시간: {(np.mean(frame_times) + np.mean(detection_times))*1000:.2f}ms")
        print(f"이론적 최대 FPS: {1/(np.mean(frame_times) + np.mean(detection_times)):.1f}")


def main():
    parser = argparse.ArgumentParser(description='YOLOv8 IMX500 안전모 탐지')
    parser.add_argument('--model', type=str, required=True,
                       help='YOLOv8 모델 경로 (.pt)')
    parser.add_argument('--conf', type=float, default=0.5,
                       help='신뢰도 임계값')
    parser.add_argument('--iou', type=float, default=0.45,
                       help='IOU 임계값')
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
    
    # 실행 모드 선택
    if args.benchmark:
        detector.benchmark()
    elif args.test_image:
        detector.test_single_image(args.test_image)
    else:
        detector.run_detection(
            display=not args.no_display,
            save_video=args.save_video,
            output_path=args.output
        )


if __name__ == "__main__":
    main()