#include <Arduino.h>

void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
  static bool on = false;
  on = !on;
  digitalWrite(LED_BUILTIN, on ? LOW : HIGH);
  Serial.print("nRF54 PlatformIO heartbeat ");
  Serial.println(millis());
  delay(1000);
}
