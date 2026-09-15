#!/usr/bin/env python3
# test_RFID.py — RC522(MFRC522) quick test for Raspberry Pi (SPI0 CE0, RST=GPIO25)
import time
import sys
import signal
import argparse
import RPi.GPIO as GPIO
from mfrc522 import SimpleMFRC522

def cleanup_and_exit(code=0):
    try:
        GPIO.cleanup()
    except Exception:
        pass
    sys.exit(code)

def main():
    ap = argparse.ArgumentParser(description="RC522 quick test")
    ap.add_argument("--once", action="store_true", help="한 번만 읽고 종료")
    ap.add_argument("--cooldown", type=float, default=2.0, help="같은 UID 재출력 억제 시간(sec)")
    args = ap.parse_args()

    # Ctrl+C 처리
    signal.signal(signal.SIGINT, lambda *_: cleanup_and_exit(0))

    # SimpleMFRC522 기본값: SPI0 CE0(GPIO8, 핀24), RST=GPIO25(핀22)
    reader = SimpleMFRC522()

    print("=== RC522 Test ===")
    print("배선: 3V3=핀17, GND=핀20, SDA/SS=GPIO8(핀24), SCK=GPIO11(핀23), MOSI=GPIO10(핀19), MISO=GPIO9(핀21), RST=GPIO25(핀22)")
    print("카드를 리더에 대세요… (종료: Ctrl+C)")

    last_uid = None
    last_ts = 0.0
    try:
        if args.once:
            uid, text = reader.read()  # 블로킹
            print(f"UID: {uid}")
            if text:
                print("TEXT:", text.strip())
            cleanup_and_exit(0)

        # 연속 모드
        while True:
            uid, text = reader.read()  # 태그 댄 순간 반환
            now = time.time()

            # 같은 카드 연타 시 중복 억제
            if uid == last_uid and (now - last_ts) < args.cooldown:
                continue
            last_uid, last_ts = uid, now

            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            t = (text or "").strip()
            print(f"[{ts_str}] UID={uid}" + (f" | TEXT='{t}'" if t else ""))

            # 안내 메시지
            print("→ 태그를 떼었다가 다시 대면 다음 읽기 됩니다.")
            time.sleep(0.2)

    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("에러:", e)
    finally:
        cleanup_and_exit(0)

if __name__ == "__main__":
    main()
