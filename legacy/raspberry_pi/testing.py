# force_reset_and_listen.py
import serial, time
P="/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_054433A4937351A0A7F5-if00"
s=serial.Serial(P,9600,timeout=0.4,exclusive=True)
# 강제 리셋 펄스
try:
    s.setDTR(True);  time.sleep(0.05)
    s.setDTR(False); time.sleep(2.5)
except: pass

print("[STEP] listening 3s for boot banner...")
t=time.time()
while time.time()-t<3:
    line=s.readline()
    if line:
        print("[BOOT]", line.decode(errors="ignore").strip())

for eol in (b'\n', b'\r', b'\r\n'):
    print(f"[SEND] RAW with EOL={eol!r}")
    s.write(b"RAW"+eol); s.flush()
    t=time.time()
    while time.time()-t<4:
        line=s.readline()
        if line: print("[RX]", line.decode(errors="ignore").strip())

print(f"[SEND] START 3 with CRLF")
s.write(b"START 3\r\n"); s.flush()
t=time.time()
while time.time()-t<8:
    line=s.readline()
    if line: print("[RX]", line.decode(errors="ignore").strip())
s.close()
