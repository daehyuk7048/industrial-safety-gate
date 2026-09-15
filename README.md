# Industrial Safety Gate

산업현장 출입 전 개인보호구(PPE) 착용 여부를 **카메라(YOLOv8) + 접촉/금속 센서(Arduino)** 로 이중 검사하는 게이트 프로젝트입니다.
Raspberry Pi 5 가 게이트 본체를 제어하고, 노트북이 YOLO 추론과 Flask 관리 서버(SQLite)를 담당합니다.

## 1. 게이트 구성

```text
            ┌──────────────────────────┐
            │  Pi Camera (상단 암, 약 60cm)  │  ← 안전모: YOLOv8 (HARDHAT)
            └────────────┬─────────────┘
                         │
   ┌─────────────────────┼─────────────────────┐
   │   RFID 리더(RC522)   │   릴레이/LED (BOARD 16) │
   │                      │                        │
   │   손잡이 GSR 전극  ───┼──▶ Arduino UNO (A0)     │  ← 안전장갑: 피부 저항 범위
   │                      │                        │
   │   바닥 금속 근접센서 ─┼──▶ Arduino UNO (D3)     │  ← 안전화: 금속 부품 감지
   └──────────────────────┴─────────────────────┘
                         │ USB Serial 9600
                         ▼
                   Raspberry Pi 5
                    ├─ TCP 카메라 스트림 :9999 (JPEG)
                    ├─ 제어 채널 :9998 (JSON Lines)
                    └─ Flask API 호출 (:5001)
                         │ Wi-Fi
                         ▼
                   Laptop / Server
                    ├─ laptop/process_client_gate.py  (YOLOv8 CPU 추론)
                    └─ server/server.py               (Flask + SQLite, 웹 관리 화면)
```

| 검사 항목 | 센서 | 위치 | 판정 주체 |
|---|---|---|---|
| 안전모 | Raspberry Pi Camera + YOLOv8 | 게이트 최상단에서 약 60cm 뻗은 암 끝 | 노트북 (`process_client_gate.py`) |
| 안전장갑 | GSR(피부 전도) 전극 | 손잡이 — 손을 올리면 저항 측정 | Arduino (`main.cpp`) |
| 안전화 | PNP 금속 근접센서 | 바닥 | Arduino (`main.cpp`) |
| 사용자 식별 | RC522 RFID 또는 QR 티켓 | 게이트 입구 | Raspberry Pi ↔ Flask |
| 출입 표시 | 릴레이/LED | 게이트 | Raspberry Pi GPIO |

## 2. 저장소 구조

```text
industrial-safety-gate/
├─ raspberry_pi/
│  ├─ gate_pi_server_all.py   # ★ 게이트 메인: 스트림·제어 서버, Arduino/RFID/QR 워커, LED
│  ├─ stream_server.py        # 카메라 스트림만 단독 실행 (디버깅용)
│  ├─ YOLO.py                 # Pi 온디바이스 YOLOv8 추론/벤치마크 (IMX500)
│  ├─ test_RFID.py            # RC522 배선·UID 확인
│  └─ tools/capture.py        # 학습 데이터셋용 영상 녹화 + 프레임 추출
├─ laptop/
│  └─ process_client_gate.py  # ★ Pi 스트림 수신 → YOLOv8 안전모 판정 → Pi에 결과 회신
├─ server/
│  ├─ server.py               # ★ Flask API + 웹 화면 (관리자/사원/QR/스캐너)
│  ├─ init_db.py              # SQLite 초기화 + 더미 데이터
│  └─ templates/*.html
├─ arduino/ppe_sensors/       # ★ PlatformIO (Arduino UNO) — GSR + 금속센서 펌웨어
│  ├─ platformio.ini
│  └─ src/main.cpp
├─ models/                    # YOLO 가중치 위치 (git 제외, README 참고)
├─ legacy/                    # 개발 과정의 이전 버전 (참고용)
├─ requirements-pi.txt
├─ requirements-laptop.txt
├─ requirements-server.txt
└─ README.md
```

## 3. 검사 흐름

1. 작업자가 RFID 카드를 태그한다 (또는 웹에서 발급한 QR 티켓을 `/api/qr/next_task` 로 Pi가 폴링).
2. Pi가 `/api/rfid/resolve` 로 사원을 조회하고 `/api/rfid/start` 로 검사 시작을 기록한다.
3. Pi가 노트북에 `{"cmd":"start","window_s":10,...}` 를 보내고, Arduino에 `START 10` 을 보낸다.
4. **10초 검사 창** 동안
   - 노트북: 스트림 프레임에 YOLOv8 추론 → 안전모(`helmet`/`hardhat` 클래스)가 보이면 `det_result` 를 1회 회신
   - Arduino: 200 ms 마다 `GSR=… | METAL=… | OK=TRUE/FALSE` 라인 출력 → Pi가 `OK=TRUE` 횟수를 누적 (9초 컷오프)
5. 창이 끝나는 시점에 Pi가 **1회** 최종 판정한다.
6. PASS 이면 릴레이/LED 를 2초 점등, FAIL 이면 소등 유지.
7. 결과를 `/api/rfid/complete` (또는 `/api/qr/complete`) 로 저장하고 관리자 화면에 표시된다.

## 4. 판정 파라미터 (코드 기준)

### Raspberry Pi — `gate_pi_server_all.py` CLI 옵션

| 옵션 | 기본값 | 의미 |
|---|---|---|
| `--window-s` | 10 | 검사 창 길이(초) |
| `--ard-cutoff-s` | 9.0 | Arduino 샘플 수집 마감(초) |
| `--ard-need-n` | 10 | PASS 에 필요한 Arduino `OK=TRUE` 누적 횟수 |
| (코드 내) `cam_need_n` | 1 | PASS 에 필요한 카메라 OK 횟수 |
| `--ok-hold-s` | 2.0 | PASS 시 LED/릴레이 점등 시간 |
| `--gpio-pin` / `--gpio-mode` | 16 / board | LED·릴레이 핀 |
| `--gpio-active-low` | off | Active-LOW 릴레이 보드일 때 지정 |
| `--width --height --fps --jpeg-q` | 640 480 15 75 | 스트림 설정 |
| `--rfid` | none | `mfrc522` (SPI RC522) / `serial` / `none` |
| `--arduino-port` | `/dev/serial/by-id/usb-Arduino…` | 사용 보드에 맞게 `/dev/ttyACM0` 등으로 지정 |
| `--server-url` | `http://127.0.0.1:5001` | Flask 서버 주소 (환경변수 `SERVER_URL`) |
| `--gate-id` | G1 | 게이트 식별자 (환경변수 `GATE_ID`) |

최종 판정식:

```text
PASS = (카메라 OK 누적 ≥ cam_need_n) AND (Arduino OK=TRUE 누적 ≥ ard_need_n)
```

Arduino 는 200 ms 주기이므로 9초 컷오프 안에 최대 45 라인이 들어오고, 그중 10 라인(≈2초) 이상 장갑+금속이 동시에 만족되어야 합니다.

### Arduino — `main.cpp`

| 항목 | 값 |
|---|---|
| GSR 핀 | A0 (노랑→A0, 흰→5V, 검정→GND) |
| GSR 판정 | `HAND ≤ 499`, `GLOVE 500~800`, `NONE ≥ 801` (5점 중위수 필터) |
| 금속센서 핀 | D3 — PNP OUT → 분압 노드 → D3, 노드→10 kΩ→GND |
| 금속 판정 | `HIGH = PASS`, 7회 샘플 중 4회 이상 HIGH (디바운스) |
| 출력 주기 | 200 ms, 9600 baud |
| OK 조건 | `GLOVE AND METAL PASS` |

GSR 임계값(499/800)과 금속센서 감도는 실험값이므로 손잡이 전극·안전화 종류에 따라 재보정이 필요합니다.

## 5. 설치 및 실행

```bash
git clone https://github.com/daehyuk7048/industrial-safety-gate.git
cd industrial-safety-gate
```

### 5-1. Arduino (PlatformIO)

`arduino/ppe_sensors/platformio.ini` 의 `upload_port` 를 실제 포트(`COM3`, `/dev/ttyACM0` 등)로 바꾼 뒤:

```bash
cd arduino/ppe_sensors
pio run -t upload
pio device monitor -b 9600
```

### 5-2. Laptop — Flask 서버

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements-server.txt

cd server
python init_db.py               # database.db 생성 + 더미 데이터
set ADMIN_PASSWORD=원하는비번     # 미지정 시 admin / admin
set SECRET_KEY=임의의긴문자열
python server.py                # http://0.0.0.0:5001
```

외부(Pi가 다른 네트워크)에서 접근해야 하면 ngrok 등으로 5001 포트를 노출합니다.

### 5-3. Laptop — YOLO 클라이언트

```bash
pip install -r requirements-laptop.txt
python laptop/process_client_gate.py --pi-ip 192.168.0.10 --model models/best.pt
```

옵션: `--conf 0.5 --iou 0.45 --imgsz 640 --skip 0` (CPU 부하가 크면 `--imgsz 416` 또는 `--skip 1`).

### 5-4. Raspberry Pi

```bash
sudo apt install -y python3-picamera2 python3-opencv
pip install -r requirements-pi.txt

python3 raspberry_pi/gate_pi_server_all.py \
    --rfid mfrc522 \
    --arduino-port /dev/ttyACM0 \
    --server-url http://<노트북IP>:5001 \
    --gate-id G1
```

RFID 없이 테스트할 때는 터미널에서 **ENTER** 를 누르면 수동으로 10초 검사가 시작됩니다 (`7` 처럼 초를 입력하면 그 길이로).

## 6. 통신 규격

### Arduino → Raspberry Pi (Serial 9600)

```text
GSR=612 (GLOVE(500-800)) | METAL=1 (PASS(H)) | OK=TRUE
```

Pi 의 파서는 대소문자를 무시하고 `key=value` 를 추출하며, `{"gsr":..,"metal":..,"ok":..}` 형태의 JSON 라인도 받습니다.
`OK=TRUE` 라인 1개 = Arduino 성공 1회로 누적됩니다. Pi가 보내는 `START n` 명령은 현재 펌웨어에서는 사용하지 않습니다(연속 출력 방식).

### Raspberry Pi ↔ Laptop (TCP :9998, JSON Lines)

Pi → Laptop

```json
{"cmd":"start","window_s":10,"uid":"1700000000","source":"rfid","badge_id":"0000000001"}
```

Laptop → Pi (검사 창 안에서 1회)

```json
{"cmd":"det_result","ok":true}
```

카메라 스트림(TCP :9999)은 `4바이트 big-endian 길이 + JPEG` 가 반복되는 형식입니다.

### Raspberry Pi → Flask API (:5001)

| 메서드 | 경로 | 요청 | 설명 |
|---|---|---|---|
| GET/POST | `/api/rfid/resolve` | `uid` | RFID UID → `{employeeId, employeeName}` |
| POST | `/api/rfid/start` | `{gateId, rfidUid, employeeId?, employeeName?}` | 검사 시작 로그(`검증중`) |
| POST | `/api/rfid/complete` | `{gateId, rfidUid, ok, helmet, metal}` | 최종 결과 로그(`양호`/`불량`) |
| POST | `/api/rfid/bind` | `{rfidUid, employeeId}` | 카드 UID 를 사번에 등록 |
| POST | `/api/qr/start` | `{employeeId, gateId}` | QR 티켓 발급 → `ticketId` |
| GET | `/api/qr/next_task?gate=G1` | — | Pi 가 1초마다 폴링하는 대기 티켓 |
| POST | `/api/qr/complete` | `{ticketId, gateId, ok, helmet, metal, source}` | QR 검사 결과 저장 |

웹 화면: `/` 관리자 대시보드(로그·팀·공지), `/login` 사원 로그인, `/qr_generator` QR 발급, `/my_work`, `/notices`, `/scanner`.

## 7. YOLO 모델 학습

학습 코드는 이 저장소에 없습니다. 데이터 수집만 Pi 에서 하고, 학습은 **Google Colab(GPU)** 에서 Ultralytics YOLOv8 로 진행한 뒤 `best.pt` 만 내려받아 배포했습니다.

### 7-1. 파이프라인

```text
Raspberry Pi                       Roboflow                    Google Colab                 배포
tools/capture.py  ──▶  프레임 추출  ──▶  라벨링 / 증강 / 분할  ──▶  yolov8s.pt 전이학습  ──▶  models/best.pt
(rpicam-vid 녹화)      (yolo_dataset/)    (data.yaml 내보내기)        (200 epochs)             노트북 CPU 추론
                                                                        └─▶ OpenVINO export  ──▶  Intel GPU (legacy)
```

1. **데이터 수집** — `raspberry_pi/tools/capture.py` 로 실제 게이트 위치(상단 암 카메라 시점)에서 영상을 녹화하고 프레임으로 추출 (`yolo_dataset/videos`, `yolo_dataset/frames/capture_YYYYMMDD_HHMMSS/`).
2. **라벨링** — Roboflow 에서 바운딩 박스 라벨링 후 YOLOv8 형식(`data.yaml`)으로 내보내기.
3. **학습** — Colab 에서 사전학습 가중치 `yolov8s.pt` 를 기반으로 전이학습.
4. **배포** — `best.pt` 를 `models/` 에 두고 노트북(`process_client_gate.py`)에서 CPU 추론. Intel 내장 GPU 를 쓰기 위해 OpenVINO 로도 변환해 봤으나 출력 차원 문제로 최종적으로는 PyTorch CPU 추론을 사용.

### 7-2. 최종 모델 학습 설정 (게이트에 배포된 `best.pt`)

| 항목 | 값 |
|---|---|
| 기반 모델 | `yolov8s.pt` (pretrained, transfer learning) |
| 데이터셋 | Roboflow `HARDHAT` v10 (`HARDHAT-10/data.yaml`) |
| 클래스 | `0: HARDHAT` (1 클래스) |
| epochs / patience | 200 / 100 |
| batch / imgsz | 10 / 640 |
| optimizer | auto (Ultralytics 기본) |
| 프레임워크 | Ultralytics 8.3.x, task=detect |
| 결과 파일 | `best.pt` 약 22 MB (옵티마이저 제거된 배포용) |

Colab 에서 사용한 학습 명령은 다음과 같습니다.

```python
from ultralytics import YOLO

model = YOLO("yolov8s.pt")
model.train(
    data="/content/HARDHAT-10/data.yaml",
    epochs=200, patience=100,
    batch=10, imgsz=640,
)
# 결과: runs/detect/train/weights/best.pt → models/best.pt 로 복사
```

OpenVINO 변환(선택):

```bash
yolo export model=models/best.pt format=openvino imgsz=640
```

### 7-3. 학습 이력

| 시기 | 데이터셋 | 클래스 | 설정 | 비고 |
|---|---|---|---|---|
| 2025-08 | `HARDHAT-10` | `HARDHAT` | yolov8s, 200 ep, batch 10 | **게이트에 최종 배포** (`models/best.pt`) |
| 2025-09 초 | `SAFETY-1` | `Safety_Helmet` | yolov8s, 100 ep, batch 10 | 자체 데이터 비교 실험 |
| 2025-09 중 | `HARDHAT` OpenVINO 변환 | `HARDHAT` | 640×640, FP32 | `legacy/laptop/process_GPU.py` 용 |
| 2025-09 중 | `Safety-1` | `Glove`, `Hardhat`, `Non-glove` | yolov8s, 200 ep 계획 (11 ep 체크포인트) | 안전장갑까지 카메라로 판정하려던 3-클래스 실험. 장갑은 최종적으로 GSR 센서로 판정 |

### 7-4. 클래스 이름 규칙

`process_client_gate.py` 는 클래스 이름에 `helmet` 또는 `hardhat` 이 포함되면(대소문자 무시) 안전모로 판정합니다. 다른 데이터셋으로 재학습해도 클래스 이름만 이 규칙에 맞으면 코드 수정 없이 교체할 수 있습니다.

가중치(`*.pt`, OpenVINO 변환본)와 학습 데이터셋은 용량 때문에 git 에 포함하지 않습니다. `models/README.md` 참고.

## 8. legacy/ 폴더

개발 과정에서 거쳐 간 버전들입니다. 메인 흐름에서는 사용하지 않습니다.

| 파일 | 내용 |
|---|---|
| `raspberry_pi/RFID_server.py` | 초기 통합 서버 — 스트림/제어/Arduino/RFID (LED·Flask 연동 없음) |
| `raspberry_pi/siyeon.py` | 중간 버전 — RelayLED 추가, Flask 연동 전 |
| `raspberry_pi/YOLO_camera_v3.py` | Camera Module V3 용 온디바이스 YOLOv8 추론 |
| `raspberry_pi/testing.py` | Arduino 시리얼 리셋·프로토콜 확인 스크립트 |
| `laptop/process_client.py` | 스트림 뷰어 + YOLO (제어 채널 없음) |
| `laptop/process_GPU.py` | OpenVINO(Intel GPU) 추론 클라이언트, `helmet/no-helmet` 2클래스 모델용 |

## 9. 주의

- 실제 게이트 릴레이에 연결하기 전에는 LED 로 충분히 테스트하세요.
- `init_db.py` 의 사원 데이터와 `server.py` 의 RFID 초기 매핑은 더미값입니다. 실제 사번·카드 UID 로 바꾸거나 `/api/rfid/bind` 로 등록하세요.
- `SECRET_KEY`, `ADMIN_PASSWORD` 는 환경변수로 지정하고 `database.db` 는 커밋하지 마세요 (`.gitignore` 처리됨).
- `server.py` 는 `debug=True` 로 실행되므로 외부 노출 시 끄는 것을 권장합니다.
