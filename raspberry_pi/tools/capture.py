#!/usr/bin/env python3
"""
rpicam 직접 호출 - 최소 필수 파라미터
"""

import subprocess
import os
import cv2
import time
from datetime import datetime

class RPiCamDirectCapture:
    def __init__(self, output_dir="yolo_dataset"):
        self.output_dir = output_dir
        self.video_dir = os.path.join(output_dir, "videos")
        self.frames_dir = os.path.join(output_dir, "frames")
        
        os.makedirs(self.video_dir, exist_ok=True)
        os.makedirs(self.frames_dir, exist_ok=True)
        
        # 시스템 검증
        self.verify_system()
    
    def verify_system(self):
        """시스템 상태 검증"""
        try:
            result = subprocess.run(['rpicam-hello', '--list'], 
                                  capture_output=True, text=True, timeout=2)
            if result.returncode != 0:
                print(f"카메라 감지 실패")
                return False
            print("카메라 감지 완료")
            return True
        except:
            print("rpicam-hello 실행 불가")
            return False
    
    def record_video_minimal(self, duration=60, filename=None):
        """libav 코덱 직접 활용"""
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"capture_{timestamp}.mp4"
        
        filepath = os.path.join(self.video_dir, filename)
        
        # 방법 1: libav 직접 출력
        cmd = [
            'rpicam-vid',
            '-t', str(duration * 1000),
            '-o', filepath,
            '--codec', 'libav',
            '--libav-format', 'mp4'
        ]
        
        print(f"실행: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0 and os.path.exists(filepath):
            print(f"성공: {filepath}")
            return filepath
        
        # 방법 2: 파이프라인 스트리밍
        print("대체 방법: 실시간 변환")
        temp_file = filepath.replace('.mp4', '_temp.h264')
        
        cmd_record = [
            'rpicam-vid',
            '-t', str(duration * 1000),
            '-o', '-',  # stdout 출력
            '--inline'
        ]
        
        cmd_encode = [
            'ffmpeg',
            '-i', '-',  # stdin 입력
            '-c:v', 'copy',
            '-f', 'mp4',
            filepath,
            '-y'
        ]
        
        # 파이프 연결
        p1 = subprocess.Popen(cmd_record, stdout=subprocess.PIPE)
        p2 = subprocess.Popen(cmd_encode, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p1.stdout.close()
        output, error = p2.communicate()
        
        if p2.returncode == 0 and os.path.exists(filepath):
            return filepath
        
        return None
    
    def convert_to_mp4(self, h264_path):
        """H264 to MP4 변환"""
        mp4_path = h264_path.replace('.h264', '.mp4')
        
        # ffmpeg 사용
        cmd = ['ffmpeg', '-i', h264_path, '-c', 'copy', mp4_path, '-y']
        result = subprocess.run(cmd, capture_output=True)
        
        if result.returncode == 0:
            os.remove(h264_path)
            return mp4_path
        
        # MP4Box 대체
        cmd_alt = ['MP4Box', '-add', h264_path, mp4_path]
        result = subprocess.run(cmd_alt, capture_output=True)
        
        if result.returncode == 0:
            os.remove(h264_path)
            return mp4_path
        
        return h264_path
    
    def test_capture_modes(self):
        """작동 가능한 모드 탐색"""
        test_results = {}
        
        # 테스트 1: 기본 스틸
        cmd = ['rpicam-still', '-o', 'test.jpg']
        result = subprocess.run(cmd, capture_output=True)
        test_results['still'] = result.returncode == 0
        
        # 테스트 2: 짧은 비디오
        cmd = ['rpicam-vid', '-t', '1000', '-o', 'test.h264']
        result = subprocess.run(cmd, capture_output=True)
        test_results['vid_basic'] = result.returncode == 0
        
        # 테스트 3: 인라인 비디오
        cmd = ['rpicam-vid', '-t', '1000', '-o', 'test2.h264', '--inline']
        result = subprocess.run(cmd, capture_output=True)
        test_results['vid_inline'] = result.returncode == 0
        
        # 정리
        for f in ['test.jpg', 'test.h264', 'test2.h264']:
            if os.path.exists(f):
                os.remove(f)
        
        return test_results
    
    def extract_frames(self, video_path, interval=1):
        """프레임 추출"""
        if not os.path.exists(video_path):
            print(f"파일 없음: {video_path}")
            return 0
        
        cap = cv2.VideoCapture(video_path)
        
        if not cap.isOpened():
            print(f"영상 열기 실패: {video_path}")
            return 0
        
        fps = int(cap.get(cv2.CAP_PROP_FPS))
        if fps == 0:
            fps = 30  # 기본값
        
        frame_interval = int(fps * interval)
        
        video_name = os.path.splitext(os.path.basename(video_path))[0]
        frames_subdir = os.path.join(self.frames_dir, video_name)
        os.makedirs(frames_subdir, exist_ok=True)
        
        frame_count = 0
        saved_count = 0
        
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            if frame_count % frame_interval == 0:
                frame_path = os.path.join(frames_subdir, f"frame_{saved_count:06d}.jpg")
                cv2.imwrite(frame_path, frame)
                saved_count += 1
            
            frame_count += 1
        
        cap.release()
        print(f"추출 완료: {saved_count}개")
        return saved_count
    
    def continuous_capture(self, count=100, interval=1):
        """연속 이미지 캡처 - 대안"""
        images_dir = os.path.join(self.output_dir, "images")
        os.makedirs(images_dir, exist_ok=True)
        
        for i in range(count):
            filepath = os.path.join(images_dir, f"img_{i:06d}.jpg")
            cmd = ['rpicam-still', '-o', filepath, '-n']
            subprocess.run(cmd, capture_output=True)
            
            if i % 10 == 0:
                print(f"진행: {i}/{count}")
            
            time.sleep(interval)
        
        return images_dir

def main():
    capture = RPiCamDirectCapture()
    
    # 시스템 테스트
    print("시스템 테스트 실행")
    test_results = capture.test_capture_modes()
    print(f"테스트 결과: {test_results}")
    
    print("\n작동 모드:")
    print("1. 영상 녹화 시도")
    print("2. 연속 이미지 캡처")
    print("3. 프레임 추출")
    
    mode = input("선택: ")
    
    if mode == "1":
        duration = int(input("시간(초): "))
        video_path = capture.record_video_minimal(duration)
        
        if video_path:
            print(f"성공: {video_path}")
            if input("프레임 추출(y/n): ").lower() == 'y':
                capture.extract_frames(video_path, 1)
        else:
            print("실패 - 대안: 연속 이미지 모드 사용")
    
    elif mode == "2":
        count = int(input("이미지 수: "))
        interval = float(input("간격(초): "))
        path = capture.continuous_capture(count, interval)
        print(f"완료: {path}")
    
    elif mode == "3":
        video_path = input("영상 경로: ")
        interval = float(input("간격(초): "))
        capture.extract_frames(video_path, interval)

if __name__ == "__main__":
    main()