#include <Arduino.h>
#include <bluefruit.h>

BLEUart uart;

void setup() {
  Serial.begin(115200);
  Bluefruit.begin();
  Bluefruit.setName("X54-PIO");
  uart.begin();
  Bluefruit.Advertising.clearData();
  Bluefruit.ScanResponse.clearData();
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addService(uart);
  // The short name fits alongside the UART UUID in the primary advertisement.
  Bluefruit.Advertising.addName();
  Bluefruit.ScanResponse.addName();
  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244);
  Bluefruit.Advertising.setFastTimeout(30);
  Bluefruit.Advertising.start(0);
}

void loop() {
  while (uart.available()) {
    Serial.write(uart.read());
  }
  if (Bluefruit.connected() && uart.notifyEnabled() && Serial.available()) {
    uart.write(Serial.read());
  }
  delay(1);
}
