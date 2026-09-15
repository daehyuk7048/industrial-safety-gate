#include <Arduino.h>

/* 핀 매핑 */
const uint8_t PIN_GSR   = A0;  // GSR: 노랑→A0, 흰→5V, 검정→GND
const uint8_t PIN_METAL = 3;   // PNP OUT → 분압 노드 → D3 (노드에서 10k→GND)

/* === GSR 범위(요청 기준) ===
 * HAND  : <= 499
 * GLOVE : 500 ~ 800 (포함)
 * NONE  : 801+
 */
const int GSR_HAND_MAX  = 499;
const int GSR_GLOVE_MIN = 500;
const int GSR_GLOVE_MAX = 800;

/* 출력 주기 */
const unsigned long PRINT_INTERVAL_MS = 200;

/* 금속센서 디바운스(간단) */
const uint8_t METAL_SAMPLES = 7;
const uint8_t METAL_PASS_MIN = 4;
const uint8_t METAL_SAMPLE_DELAY_MS = 2;

/* GSR 중위수 필터(노이즈 억제) */
int median5() {
  int v[5];
  for (int i=0;i<5;i++){ v[i]=analogRead(PIN_GSR); delay(2); }
  for (int i=1;i<5;i++){
    int key=v[i], j=i-1;
    while(j>=0 && v[j]>key){ v[j+1]=v[j]; j--; }
    v[j+1]=key;
  }
  return v[2];
}

bool metalPassDebounced() {
  uint8_t high_cnt=0;
  for(uint8_t i=0;i<METAL_SAMPLES;i++){
    if (digitalRead(PIN_METAL)==HIGH) high_cnt++;  // PNP=HIGH → PASS
    delay(METAL_SAMPLE_DELAY_MS);
  }
  return (high_cnt>=METAL_PASS_MIN);
}

enum GsrClass { G_NONE, G_HAND, G_GLOVE };

GsrClass classifyGSR(int raw){
  if (raw >= GSR_GLOVE_MIN && raw <= GSR_GLOVE_MAX) return G_GLOVE;
  if (raw <= GSR_HAND_MAX)                           return G_HAND;
  return G_NONE; // 801+
}

const char* gsrName(GsrClass c){
  switch(c){
    case G_HAND:  return "HAND(<=499)";
    case G_GLOVE: return "GLOVE(500-800)";
    default:      return "NONE(801+)";
  }
}

void setup(){
  Serial.begin(9600);
  delay(200);
  pinMode(PIN_METAL, INPUT);  // 외부 분압 사용
  Serial.println(F("=== SENSOR TEST ==="));
  Serial.println(F("GSR: HAND<=499, GLOVE=500-800, NONE=801+"));
  Serial.println(F("METAL: PNP HIGH=PASS (분압 노드→D3, 노드→10k→GND)"));
  Serial.println();
}

void loop(){
  static unsigned long last=0;
  if (millis()-last < PRINT_INTERVAL_MS) return;
  last = millis();

  // GSR 읽기/분류
  int gsr_raw = median5();
  GsrClass gsr = classifyGSR(gsr_raw);

  // 금속(PNP) 판정
  bool metal_pass = metalPassDebounced();
  int  metal_raw  = digitalRead(PIN_METAL); // 1=PASS(H), 0=NONE(L)

  // 최종 OK = 장갑 && 금속 PASS
  bool ok = (gsr==G_GLOVE) && metal_pass;

  // 라즈베리파이 파서 호환 출력
  Serial.print(F("GSR="));   Serial.print(gsr_raw);
  Serial.print(F(" ("));     Serial.print(gsrName(gsr)); Serial.print(F(") | "));
  Serial.print(F("METAL=")); Serial.print(metal_raw);
  Serial.print(F(" ("));     Serial.print(metal_pass?F("PASS(H)"):F("NONE(L)")); Serial.print(F(") | "));
  Serial.print(F("OK="));    Serial.println(ok?F("TRUE"):F("FALSE"));
}
