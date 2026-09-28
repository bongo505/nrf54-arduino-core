#!/usr/bin/env python3
"""Execute Wire's write-address-only scanner probes against an open-drain bus."""

from __future__ import annotations

from pathlib import Path
import os
import shlex
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "hardware/nrf54l15clean/nrf54l15clean/cores"
WIRE_SOURCES = (
    CORE / "nrf54l15/Wire.cpp",
    CORE / "nrf54lm20b/Wire.cpp",
)
SCANNER = (
    ROOT
    / "hardware/nrf54l15clean/nrf54l15clean/examples/Wire/WireScanner/WireScanner.ino"
)


def function_body(text: str, signature: str) -> str:
    start = text.find(signature)
    assert start >= 0, f"missing function: {signature}"
    opening = text.find("{", start)
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index]
    raise AssertionError(f"unterminated function: {signature}")


def braced_block(text: str, token: str) -> str:
    start = text.find(token)
    assert start >= 0, f"missing block: {token}"
    opening = text.find("{", start + len(token))
    assert opening >= 0, f"missing opening brace: {token}"
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated block: {token}")


def assert_order(text: str, *tokens: str) -> None:
    cursor = 0
    for token in tokens:
        offset = text.find(token, cursor)
        assert offset >= 0, f"missing ordered token: {token}"
        cursor = offset + len(token)


def end_tx_error_code(
    transaction_ok: bool, stop_ok: bool, errorsrc: int, error_event: bool = False
) -> int:
    transaction_ok = transaction_ok and not error_event
    if transaction_ok and stop_ok and errorsrc == 0:
        return 0
    if errorsrc & (1 << 1):
        return 2
    if errorsrc & (1 << 2):
        return 3
    return 4


def test_error_mapping_model() -> None:
    assert end_tx_error_code(True, True, 0) == 0
    assert end_tx_error_code(False, True, 1 << 1) == 2
    assert end_tx_error_code(False, True, 1 << 2) == 3
    assert end_tx_error_code(False, False, 0) == 4
    assert end_tx_error_code(True, True, 0, error_event=True) == 4
    print("PASS Wire address/data NACK and timeout status model")


BUS_HARNESS = r"""
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <vector>

constexpr uint32_t GPIO_PIN_CNF_DIR_Pos = 0, GPIO_PIN_CNF_DIR_Msk = 1;
constexpr uint32_t GPIO_PIN_CNF_DIR_Input = 0;
constexpr uint32_t GPIO_PIN_CNF_INPUT_Msk = 2, GPIO_PIN_CNF_INPUT_Connect = 0;
constexpr uint32_t GPIO_PIN_CNF_PULL_Msk = 12, GPIO_PIN_CNF_PULL_Disabled = 0;
constexpr uint32_t GPIO_PIN_CNF_DRIVE0_Pos = 8, GPIO_PIN_CNF_DRIVE0_Msk = 0x300;
constexpr uint32_t GPIO_PIN_CNF_DRIVE1_Pos = 10, GPIO_PIN_CNF_DRIVE1_Msk = 0xC00;
constexpr uint32_t GPIO_PIN_CNF_DRIVE0_S0 = 0, GPIO_PIN_CNF_DRIVE1_D1 = 2;
constexpr uint32_t T_ENABLE = 0x500, T_ENABLE_DISABLED = 0;
constexpr uintptr_t peripheral = 0x1000;
constexpr unsigned sdaPin = 3, sclPin = 7, otherPin = 20;
constexpr uint32_t sdaMask = 1U << sdaPin, sclMask = 1U << sclPin;
constexpr uint32_t busMask = sdaMask | sclMask;

enum Kind { Out, Set, Clear, DirSet, DirClear, Input, Config, Enable };
struct Register {
  Kind kind = Out;
  unsigned port = 0, pin = 0;
  Register& operator=(uint32_t value);
  operator uint32_t() const;
};
struct NRF_GPIO_Type {
  Register OUT, OUTSET, OUTCLR, DIRSET, DIRCLR, IN, PIN_CNF[32];
};
static NRF_GPIO_Type ports[4];
static NRF_GPIO_Type* NRF_P0 = &ports[0];
static NRF_GPIO_Type* NRF_P1 = &ports[1];
static NRF_GPIO_Type* NRF_P2 = &ports[2];
[[maybe_unused]] static NRF_GPIO_Type* NRF_P3 = &ports[3];
static Register enableRegister{Enable, 0, 0};
static uint32_t output[4], configuration[4][32], enableValue;
static uint32_t originalOutput[4], originalConfiguration[4][32];
static uint64_t elapsed, stretchUntil, lastFall, lastRise, lastDataChange, startTime;
static unsigned selectedPort, starts, stops, releases, ackReads, registerWrites;
static unsigned stretchAt, stretchDuration;
static bool active, sclLevel, sdaLevel, releasedScl;
static bool targetAcks, slaveAck, stuckScl, stuckSda, stuckStopSda, mutateOther;
static bool quantizedDelay;
static std::vector<bool> bits;

static bool released(unsigned pin) {
  if (enableValue != T_ENABLE_DISABLED) return true;
  const uint32_t cnf = configuration[selectedPort][pin];
  if (!(cnf & GPIO_PIN_CNF_DIR_Msk)) return true;
  // The master must never actively drive either line high.
  assert((cnf & GPIO_PIN_CNF_INPUT_Msk) == GPIO_PIN_CNF_INPUT_Connect);
  assert((cnf & GPIO_PIN_CNF_DRIVE0_Msk) == 0);
  assert((cnf & GPIO_PIN_CNF_DRIVE1_Msk) ==
         (GPIO_PIN_CNF_DRIVE1_D1 << GPIO_PIN_CNF_DRIVE1_Pos));
  return (output[selectedPort] & (1U << pin)) != 0;
}

static void refresh() {
  const bool releaseClock = released(sclPin);
  if (active && releaseClock && !releasedScl) {
    ++releases;
    if (releases == stretchAt) stretchUntil = elapsed + stretchDuration;
  }
  releasedScl = releaseClock;
  const bool newScl = releaseClock && !stuckScl && elapsed >= stretchUntil;
  if (active && sclLevel && !newScl) {
    assert(elapsed - lastRise >= 5 || bits.empty());
    if (bits.empty()) assert(elapsed - startTime >= 5);
    lastFall = elapsed;
    if (bits.size() == 8) slaveAck = targetAcks;
    if (bits.size() >= 9) slaveAck = false;
  }
  if (active && !sclLevel && newScl && bits.size() == 9 && stuckStopSda) {
    stuckSda = true;
  }
  const bool newSda = released(sdaPin) && !stuckSda && !slaveAck;
  if (active && !sclLevel && newScl) {
    assert(elapsed - lastFall >= 5);
    assert(elapsed - lastDataChange >= 5);
    lastRise = elapsed;
    bits.push_back(newSda);
    if (bits.size() == 9 && stretchDuration < 25000)
      assert(released(sdaPin));  // ACK belongs to target in a completed address.
  }
  if (sclLevel && newScl && sdaLevel != newSda) {
    if (!newSda) {
      assert(!active);
      ++starts;
      active = true;
      startTime = elapsed;
      bits.clear();
      releases = 0;
      lastRise = elapsed;
    } else {
      assert(active);
      assert(elapsed - lastRise >= 5);
      ++stops;
      active = false;
    }
  }
  if (sdaLevel != newSda) lastDataChange = elapsed;
  sclLevel = newScl;
  sdaLevel = newSda;
}

Register& Register::operator=(uint32_t value) {
  ++registerWrites;
  switch (kind) {
    case Out: output[port] = value; break;
    case Set: output[port] |= value; break;
    case Clear: output[port] &= ~value; break;
    case DirSet:
    case DirClear:
      for (unsigned p = 0; p < 32; ++p) {
        if (value & (1U << p)) {
          if (kind == DirSet) configuration[port][p] |= GPIO_PIN_CNF_DIR_Msk;
          else configuration[port][p] &= ~GPIO_PIN_CNF_DIR_Msk;
        }
      }
      break;
    case Config: configuration[port][pin] = value; break;
    case Enable: enableValue = value; break;
    case Input: assert(false);
  }
  refresh();
  return *this;
}
Register::operator uint32_t() const {
  if (kind == Out) return output[port];
  if (kind == Config) return configuration[port][pin];
  if (kind == Enable) return enableValue;
  assert(kind == Input);
  refresh();
  if (active && bits.size() == 9 && sclLevel) ++ackReads;
  return (~busMask) | (sclLevel ? sclMask : 0) | (sdaLevel ? sdaMask : 0);
}
static Register& reg32(uintptr_t address) {
  assert(address == peripheral + T_ENABLE);
  return enableRegister;
}
static bool decode_pin(uint8_t pin, uint8_t* port, uint8_t* number) {
  if (pin == 255) return false;
  *port = pin / 32;
  *number = pin % 32;
  return true;
}
static uint32_t micros() {
  ++elapsed;  // A real busy wait advances time, including through uint32 wrap.
  refresh();
  return static_cast<uint32_t>(elapsed);
}
static void delayMicroseconds(uint32_t us) {
  elapsed += us - (quantizedDelay && us != 0 ? 1U : 0U);
  if (mutateOther) {
    output[selectedPort] ^= 1U << otherPin;  // An unrelated ISR updates another pin.
    originalOutput[selectedPort] ^= 1U << otherPin;
    mutateOther = false;
  }
  refresh();
}
static void __DSB() {}

// PRODUCTION_FUNCTIONS

static void reset(unsigned port, uint32_t savedOutput, bool ack) {
  selectedPort = port;
  for (unsigned i = 0; i < 4; ++i) {
    ports[i].OUT = Register{Out, i, 0};
    ports[i].OUTSET = Register{Set, i, 0};
    ports[i].OUTCLR = Register{Clear, i, 0};
    ports[i].DIRSET = Register{DirSet, i, 0};
    ports[i].DIRCLR = Register{DirClear, i, 0};
    ports[i].IN = Register{Input, i, 0};
    originalOutput[i] = output[i] = savedOutput;
    for (unsigned pin = 0; pin < 32; ++pin) {
      ports[i].PIN_CNF[pin] = Register{Config, i, pin};
      originalConfiguration[i][pin] = configuration[i][pin] =
          0xA0000 | (pin << 12) | ((pin & 3) << 2) | GPIO_PIN_CNF_INPUT_Msk;
    }
  }
  enableValue = 6;
  elapsed = 1000;
  stretchUntil = 0;
  starts = stops = releases = ackReads = registerWrites = 0;
  stretchAt = stretchDuration = 0;
  active = slaveAck = stuckScl = stuckSda = stuckStopSda = mutateOther = quantizedDelay = false;
  sclLevel = sdaLevel = releasedScl = true;
  targetAcks = ack;
  lastFall = lastRise = lastDataChange = startTime = 0;
  bits.clear();
}
static uint8_t probe(uint8_t address) {
  return twim_probe_write_address(peripheral, selectedPort * 32 + sdaPin,
                                 selectedPort * 32 + sclPin, address);
}
static void checkRestored() {
  assert(enableValue == 6);
  for (unsigned i = 0; i < 4; ++i) {
    assert(output[i] == originalOutput[i]);
    for (unsigned pin = 0; pin < 32; ++pin)
      assert(configuration[i][pin] == originalConfiguration[i][pin]);
  }
}
static void checkFrame(uint8_t address, bool ack) {
  assert(starts == 1 && stops == 1 && !active);
  // Nine address/ACK clocks, followed only by the SCL rise that forms STOP.
  assert(bits.size() == 10 && releases == 10);
  uint8_t byte = 0;
  for (unsigned bit = 0; bit < 8; ++bit) byte = (byte << 1) | bits[bit];
  assert(byte == static_cast<uint8_t>(address << 1));
  assert(!bits[7]);  // Write direction, never a one-byte read.
  assert(bits[8] == !ack && !bits[9]);
  assert(ackReads != 0 && sclLevel && sdaLevel);
  checkRestored();
}
int main() {
  for (unsigned port = 0; port < /* PORT_COUNT */; ++port) {
    for (unsigned address = 0; address < 128; ++address) {
      for (bool ack : {false, true}) {
        reset(port, (address & 1) ? 0xA5555555 : 0x5AAAAAAA, ack);
        mutateOther = true;
        quantizedDelay = address & 1;
        assert(probe(address) == (ack ? 0 : 2));
        checkFrame(address, ack);
      }
    }
  }
  reset(1, 0, true);
  assert(probe(0x40) == 0);  // SI7021/HTU21D must see the byte 0x80, not 0x81.
  checkFrame(0x40, true);
  starts = stops = ackReads = 0;
  targetAcks = false;
  assert(probe(0x41) == 2);  // A second probe reuses restored pins/peripheral cleanly.
  checkFrame(0x41, false);
  // Every address, ACK, and STOP clock can be stretched by the target.
  for (unsigned edge = 1; edge <= 10; ++edge) {
    reset(0, ~0U, true);
    stretchAt = edge;
    stretchDuration = 100;
    assert(probe(0x40) == 0);
    checkFrame(0x40, true);
    reset(2, 0, true);
    elapsed = UINT32_MAX - 20U;
    stretchAt = edge;
    stretchDuration = 26000;
    const auto before = elapsed;
    assert(probe(0x40) == 4);
    assert(elapsed - before >= 25000 && elapsed - before < 51000);
    checkRestored();
    reset(1, ~0U, true);
    stretchAt = edge;
    stretchDuration = 100000;  // Neither the original clock nor abort STOP can rise.
    const auto stuckBefore = elapsed;
    assert(probe(0x40) == 4);
    assert(elapsed - stuckBefore >= (edge == 10 ? 25000U : 50000U));
    assert(elapsed - stuckBefore < 51000);
    checkRestored();
  }
  for (bool sclFault : {false, true}) {
    reset(1, 0xA5A5A5A5, true);
    stuckScl = sclFault;
    stuckSda = !sclFault;
    sclLevel = !stuckScl;
    sdaLevel = !stuckSda;
    const auto before = elapsed;
    assert(probe(0x40) == 4);
    assert(starts == 0 && stops == 0 && bits.empty());
    assert(elapsed - before < 25100);
    if (sclFault) assert(elapsed - before >= 25000);
    checkRestored();
    stuckScl = stuckSda = false;
    sclLevel = sdaLevel = true;
    assert(probe(0x40) == 0);
    checkFrame(0x40, true);
  }
  reset(0, ~0U, true);
  stuckStopSda = true;
  assert(probe(0x40) == 4);  // ACK alone is insufficient: STOP must release SDA.
  checkRestored();
  // Invalid maps must not touch TWIM or GPIO at all.
  for (auto pins : {std::vector<uint8_t>{255, 7}, {3, 255}, {3, 39}, {3, 3}, {131, 135}}) {
    reset(0, 0, true);
    assert(twim_probe_write_address(peripheral, pins[0], pins[1], 0x40) == 4);
    assert(registerWrites == 0 && starts == 0);
    checkRestored();
  }
  std::puts("PASS production write-only probe bytes, ACK/NACK, open-drain timing, stretching, faults, and cleanup");
}
"""


def test_production_probe() -> None:
    signatures = (
        "static NRF_GPIO_Type* gpio_for_port(uint8_t port)",
        "static void configure_i2c_pin(uint8_t port, uint8_t pin)",
        "static bool twim_probe_wait_high(NRF_GPIO_Type* gpio, uint32_t mask)",
        "static uint8_t twim_probe_write_address(uintptr_t base, uint8_t sda,",
    )
    compiler = shlex.split(os.environ.get("CXX", "c++"))
    with tempfile.TemporaryDirectory(prefix="wire-probe-contracts-") as directory:
        for path in WIRE_SOURCES:
            source = path.read_text(encoding="utf-8")
            extracted = []
            for signature in signatures:
                start = source.index(signature)
                opening = source.index("{", start)
                declaration = source[start:opening]
                extracted.append(declaration + "{" + function_body(source, signature) + "}")
            cpp = Path(directory) / f"{path.parent.name}.cpp"
            executable = cpp.with_suffix("")
            harness = BUS_HARNESS.replace("// PRODUCTION_FUNCTIONS", "\n".join(extracted))
            cpp.write_text(harness.replace("/* PORT_COUNT */", "4" if "case 3:" in extracted[0] else "3"))
            subprocess.run(
                compiler + ["-std=c++17", "-Wall", "-Wextra", "-Werror", str(cpp), "-o", str(executable)],
                check=True,
            )
            subprocess.run([str(executable)], check=True, timeout=15)


def test_zero_length_probe_source() -> None:
    probe_bodies: list[str] = []
    for path in WIRE_SOURCES:
        text = path.read_text(encoding="utf-8")
        assert "T_TWIM_ERRORSRC_ANACK = (1UL << 1U)" in text
        assert "T_TWIM_ERRORSRC_DNACK = (1UL << 2U)" in text
        assert "T_TWIM_SHORT_LASTTX_STOP = (1UL << 9U)" in text
        status_helper = function_body(
            text,
            "static uint8_t end_tx_error_code(bool transactionOk, bool stopOk, uint32_t errorsrc)",
        )
        assert_order(
            status_helper,
            "transactionOk && stopOk && errorsrc == 0U",
            "return 0U",
            "errorsrc & T_TWIM_ERRORSRC_ANACK",
            "return 2U",
            "errorsrc & T_TWIM_ERRORSRC_DNACK",
            "return 3U",
            "return 4U",
        )
        body = function_body(text, "uint8_t TwoWire::endTransmission(bool sendStop)")
        probe = braced_block(body, "if (_txBufferLength == 0U)")
        probe_bodies.append(probe)

        assert "if (!sendStop || _pendingRepeatedStart)" in probe
        assert "return 4U;" in probe
        unsupported = braced_block(probe, "if (!sendStop || _pendingRepeatedStart)")
        assert_order(
            unsupported,
            "if (_pendingRepeatedStart)",
            "T_EVENTS_STOPPED) = 0U",
            "T_TASKS_STOP) = 1U",
            "wait_event(base, T_EVENTS_STOPPED",
            "_pendingRepeatedStart = !stopped",
            "return 4U;",
        )
        assert "twim_probe_write_address(" in probe
        assert "_txBuffer" not in probe[len("if (_txBufferLength == 0U)") :]
        assert "T_TASKS_DMA_RX_START" not in probe
        assert "T_TASKS_DMA_TX_START" not in probe
        assert "twim_probe_write_address(" not in unsupported
        assert_order(probe, unsupported, "twim_probe_write_address(")

        regular = body[body.index(probe) + len(probe) :]
        assert_order(
            regular,
            "T_SHORTS) = sendStop ? T_TWIM_SHORT_LASTTX_STOP : 0U",
            "wait_event_or_error(base, doneEvent",
            "uint32_t errorsrc",
            "bool errorEvent",
            "T_TASKS_STOP) = 1U",
            "wait_event(base, T_EVENTS_STOPPED",
            "errorsrc |= reg32(base + T_TWIM_ERRORSRC)",
            "errorEvent = errorEvent ||",
            "T_SHORTS) = 0U",
            "end_tx_error_code(writeOk && !errorEvent, stopOk, errorsrc)",
        )
        assert "_pendingRepeatedStart = !stopOk;" in regular
        read = function_body(
            text, "uint8_t TwoWire::requestFrom(uint8_t address, size_t quantity, bool sendStop)"
        )
        assert "_pendingRepeatedStart = !stopOk;" in read
        assert "twim_probe_write_address(" not in read

    # Board cores must not drift in scanner semantics.
    normalized = ["\n".join(line.strip() for line in body.splitlines()) for body in probe_bodies]
    assert normalized[0] == normalized[1]
    print("PASS L15 and LM20A empty writes probe W=0; DMA payload/repeated-start paths stay isolated")


def test_dual_bus_scanner_source() -> None:
    scanner = SCANNER.read_text(encoding="utf-8")
    for token in (
        "static void scanBus(TwoWire& bus, const char* name, uint8_t sda, uint8_t scl)",
        "digitalRead(sda) == LOW || digitalRead(scl) == LOW",
        'scanBus(Wire, "Wire", SDA, SCL)',
        'scanBus(Wire1, "Wire1", SDA1, SCL1)',
        "BoardControl::setImuMicEnabled(true)",
        "pinMode(PIN_IMU_CS, OUTPUT)",
        "digitalWrite(PIN_IMU_CS, HIGH)",
        "ARDUINO_XIAO_NRF54L15_CLEAN",
    ):
        assert token in scanner, f"WireScanner board-aware contract missing: {token}"
    assert scanner.count("BoardControl::setImuMicEnabled(true)") == 2
    print("PASS WireScanner powers Sense rails, selects LM20A I2C mode, and scans both buses")


def main() -> None:
    test_error_mapping_model()
    test_production_probe()
    test_zero_length_probe_source()
    test_dual_bus_scanner_source()
    print("PASS all Wire address-probe contracts")


if __name__ == "__main__":
    main()
