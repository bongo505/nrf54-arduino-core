#!/usr/bin/env python3
"""Execute the production HAL write path against mock TWIM registers."""

from pathlib import Path
import os
import shlex
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / (
    "hardware/nrf54l15clean/nrf54l15clean/libraries/"
    "Nrf54L15-Clean-Implementation/src/nrf54l15_hal_parts/"
    "nrf54l15_hal_peripherals.inc"
)


def production_write() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    start = text.index("bool Twim::write(")
    opening = text.index("{", start)
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError("unterminated Twim::write")


def main() -> None:
    harness = r"""
#include <array>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <limits>

namespace twim {
constexpr uint32_t ADDRESS = 0;
constexpr uint32_t DMA_TX_PTR = 1;
constexpr uint32_t DMA_TX_MAXCNT = 2;
constexpr uint32_t TASKS_DMA_TX_START = 3;
constexpr uint32_t EVENTS_LASTTX = 4;
constexpr uint32_t EVENTS_ERROR = 5;
constexpr uint32_t TASKS_STOP = 6;
constexpr uint32_t EVENTS_STOPPED = 7;
constexpr uint32_t ERRORSRC = 8;
constexpr uint32_t ERRORSRC_ALL = 7;
}

constexpr uint32_t kBase = 0x1000;
constexpr uint32_t kSpinLimit = 1234;
static std::array<uint32_t, 9> registers{};
static unsigned accesses;
static unsigned clears;
static unsigned transferWaits;
static unsigned stopWaits;
static bool transferDone;
static bool stopDone;
static bool earlyError;
static bool lateError;
static const uint8_t* expectedData;
static size_t expectedLength;
static uint8_t expectedAddress;

static volatile uint32_t& reg32(uint32_t address) {
  ++accesses;
  assert(address >= kBase && address < kBase + registers.size());
  return registers[address - kBase];
}

static void clearTwimState(uint32_t base) {
  assert(base == kBase);
  ++clears;
  registers.fill(0);
}

static bool waitForEventOrError(uint32_t base, uint32_t event,
                                uint32_t error, uint32_t spinLimit) {
  ++transferWaits;
  assert(base == kBase && event == twim::EVENTS_LASTTX);
  assert(error == twim::EVENTS_ERROR && spinLimit == kSpinLimit);
  assert(clears == 1);
  assert(registers[twim::TASKS_DMA_TX_START] == 1);
  assert(registers[twim::DMA_TX_PTR] ==
         static_cast<uint32_t>(reinterpret_cast<uintptr_t>(expectedData)));
  assert(registers[twim::DMA_TX_MAXCNT] == expectedLength);
  assert(registers[twim::ADDRESS] == (expectedAddress & 0x7F));
  assert(registers[twim::TASKS_STOP] == 0);
  registers[twim::EVENTS_ERROR] = earlyError;
  return transferDone;
}

static bool waitForEvent(uint32_t base, uint32_t event, uint32_t spinLimit) {
  ++stopWaits;
  assert(base == kBase && event == twim::EVENTS_STOPPED);
  assert(spinLimit == kSpinLimit && transferWaits == 1);
  assert(registers[twim::TASKS_STOP] == 1);
  registers[twim::EVENTS_ERROR] |= lateError;
  return stopDone;
}

class Twim {
 public:
  bool write(uint8_t address7, const uint8_t* data, size_t len,
             uint32_t spinLimit);
 private:
  uint32_t base_ = kBase;
};
""" + production_write() + r"""

static void reset() {
  registers.fill(0xBAD);
  accesses = clears = transferWaits = stopWaits = 0;
  transferDone = stopDone = true;
  earlyError = lateError = false;
}

int main() {
  Twim bus;
  static uint8_t buffer[65535]{};
  const size_t lengths[] = {0, 1, 65535, 65536,
                           std::numeric_limits<size_t>::max()};
  for (size_t length : lengths) {
    reset();
    assert(!bus.write(0x40, nullptr, length, kSpinLimit));
    assert(accesses == 0 && clears == 0);
    assert(transferWaits == 0 && stopWaits == 0);
    for (auto value : registers) assert(value == 0xBAD);
    if (length == 0 || length > 65535) {
      reset();
      assert(!bus.write(0x40, buffer, length, kSpinLimit));
      assert(accesses == 0 && clears == 0);
      assert(transferWaits == 0 && stopWaits == 0);
      for (auto value : registers) assert(value == 0xBAD);
    }
  }

  // Both DMA length boundaries and all controller completion/error outcomes.
  for (size_t length : {size_t(1), size_t(65535)}) {
    for (unsigned outcomes = 0; outcomes < 16; ++outcomes) {
      reset();
      expectedData = buffer;
      expectedLength = length;
      expectedAddress = 0xC0;
      transferDone = (outcomes & 1) != 0;
      stopDone = (outcomes & 2) != 0;
      earlyError = (outcomes & 4) != 0;
      lateError = (outcomes & 8) != 0;
      assert(bus.write(expectedAddress, buffer, length, kSpinLimit) ==
             (transferDone && stopDone && !earlyError && !lateError));
      assert(clears == 1 && transferWaits == 1 && stopWaits == 1);
      assert(registers[twim::ERRORSRC] == twim::ERRORSRC_ALL);
    }
  }
}
"""
    with tempfile.TemporaryDirectory(prefix="nrf54-twim-write-") as temporary:
        cpp = Path(temporary) / "twim_write.cpp"
        binary = Path(temporary) / "twim_write"
        cpp.write_text(harness, encoding="utf-8")
        subprocess.run(
            shlex.split(os.environ.get("CXX", "c++"))
            + ["-std=c++17", "-Wall", "-Wextra", "-Werror", str(cpp), "-o", str(binary)],
            check=True,
        )
        subprocess.run([str(binary)], check=True)
    print("PASS HAL TWIM invalid/null/empty DMA writes make no hardware accesses")
    print("PASS HAL TWIM payload bounds, STOP cleanup, errors and timeouts")


if __name__ == "__main__":
    main()
