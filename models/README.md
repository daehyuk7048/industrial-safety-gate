# models/

YOLO 가중치는 용량 문제로 git 에 포함하지 않습니다. 이 폴더에 직접 넣어서 사용하세요.
학습 과정은 루트 `README.md` 7장 참고.

| 파일 | 용도 | 비고 |
|---|---|---|
| `best.pt` | `laptop/process_client_gate.py`, `raspberry_pi/YOLO.py` | YOLOv8s, 클래스 `0: HARDHAT`, 약 22 MB — **게이트 배포 모델** |
| `best_openvino_model/` (`best.xml` + `best.bin`) | `legacy/laptop/process_GPU.py` | OpenVINO 변환본, 640×640 |

## 배포 모델 정보

- 기반: `yolov8s.pt` 전이학습 (Google Colab)
- 데이터셋: Roboflow `HARDHAT` v10 (`HARDHAT-10/data.yaml`)
- 설정: epochs 200, patience 100, batch 10, imgsz 640
- 프레임워크: Ultralytics 8.3.x

`process_client_gate.py` 는 클래스 이름에 `helmet` 또는 `hardhat` 이 포함되면 안전모로 판정합니다.

## OpenVINO 변환 (선택)

```bash
yolo export model=models/best.pt format=openvino imgsz=640
```
