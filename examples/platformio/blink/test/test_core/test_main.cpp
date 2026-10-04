#include <Arduino.h>
#include <unity.h>

void setUp() {}
void tearDown() {}

void test_selected_clock() {
  TEST_ASSERT_EQUAL_UINT32(F_CPU, nrf54l15_core_get_cpu_frequency_hz());
}

void test_delay_advances_time() {
  const uint32_t started = millis();
  delay(20);
  const uint32_t elapsed = millis() - started;
  TEST_ASSERT_GREATER_OR_EQUAL_UINT32(20, elapsed);
  TEST_ASSERT_LESS_THAN_UINT32(1000, elapsed);
}

void setup() {
  // The external USB serial bridge does not reset the target when opened.
  // Let the test runner attach after the upload/reset process exits.
  delay(5000);
  UNITY_BEGIN();
  RUN_TEST(test_selected_clock);
  RUN_TEST(test_delay_advances_time);
  UNITY_END();
}

void loop() {
  delay(1000);
}
