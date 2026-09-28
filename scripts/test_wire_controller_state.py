#!/usr/bin/env python3
"""Execute production Wire controller paths against a TWIM task/event harness."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CORES = ROOT / "hardware/nrf54l15clean/nrf54l15clean/cores"


def extract_function(source: str, signature: str) -> str:
    start = source.find(signature)
    assert start >= 0, f"missing function: {signature}"
    opening = source.find("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function: {signature}")


HARNESS = r"""
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <initializer_list>

@CONSTANTS@
@BUFFER_LENGTH@

constexpr uintptr_t peripheral = 0x1000;
struct NRF_TWIM_Type {};
static uint32_t values[0x800 / 4];
static unsigned registerWrites, txStarts, rxStarts, stopTasks, stopAttempts;
static unsigned probeCalls, beginCalls;
static bool transferCompletes, stopCompletes, eventError, beginSucceeds;
static uint32_t immediateError, stoppedError;
static uint8_t probeResult, observedAddress, observedSda, observedScl;
static uint32_t now;

static void finish_stop() {
    ++stopAttempts;
    if (stopCompletes) values[T_EVENTS_STOPPED / 4] = 1;
}

struct Register {
    uintptr_t offset;
    Register& operator=(uint32_t value) {
        ++registerWrites;
        if (offset == T_TWIM_ERRORSRC) {
            values[offset / 4] &= ~value;
        } else {
            values[offset / 4] = value;
        }
        if (offset == T_TASKS_STOP && value != 0) {
            ++stopTasks;
            finish_stop();
        }
        if ((offset == T_TASKS_DMA_TX_START || offset == T_TASKS_DMA_RX_START) &&
            value != 0) {
            const bool tx = offset == T_TASKS_DMA_TX_START;
            if (tx) ++txStarts; else ++rxStarts;
            if (transferCompletes)
                values[(tx ? T_EVENTS_LASTTX : T_EVENTS_LASTRX) / 4] = 1;
            values[T_TWIM_ERRORSRC / 4] |= immediateError;
            if (eventError || immediateError != 0) values[T_EVENTS_ERROR / 4] = 1;
            if (tx && transferCompletes &&
                (values[T_SHORTS / 4] & T_TWIM_SHORT_LASTTX_STOP) != 0)
                finish_stop();
        }
        return *this;
    }
    operator uint32_t() const {
        // LASTTX precedes the last byte's ACK. Expose a late NACK only when
        // STOPPED is observed, after the controller's first error snapshot.
        if (offset == T_EVENTS_STOPPED && values[offset / 4] != 0 &&
            stoppedError != 0) {
            values[T_TWIM_ERRORSRC / 4] |= stoppedError;
            values[T_EVENTS_ERROR / 4] = 1;
        }
        return values[offset / 4];
    }
};

static Register reg32(uintptr_t address) {
    assert(address >= peripheral && address < peripheral + sizeof(values));
    assert((address & 3U) == 0);
    return Register{address - peripheral};
}
static uint32_t micros() { return ++now; }
static uint8_t twim_probe_write_address(uintptr_t base, uint8_t sda,
                                        uint8_t scl, uint8_t address) {
    assert(base == peripheral);
    ++probeCalls;
    observedAddress = address;
    observedSda = sda;
    observedScl = scl;
    return probeResult;
}

@HELPERS@

class TwoWire {
public:
    NRF_TWIM_Type* _twim = reinterpret_cast<NRF_TWIM_Type*>(peripheral);
    uint8_t _sda = 4, _scl = 5;
    bool _initialized = true, _targetRegistered = false;
    uint8_t _txBuffer[BUFFER_LENGTH] = {}, _txBufferLength = 0, _txAddress = 0x40;
    uint8_t _rxBuffer[BUFFER_LENGTH] = {}, _rxBufferLength = 0, _rxBufferIndex = 0;
    int _peek = -1;
    bool _pendingRepeatedStart = false;
    uint32_t _lastActivityUs = 0;

    void begin() { ++beginCalls; _initialized = beginSucceeds; }
    void clearReceiveState() {
        _rxBufferLength = 0;
        _rxBufferIndex = 0;
        _peek = -1;
    }
    uint8_t endTransmission(bool sendStop);
    uint8_t requestFrom(uint8_t address, size_t quantity, bool sendStop);
};

@METHODS@

static void reset() {
    std::memset(values, 0, sizeof(values));
    registerWrites = txStarts = rxStarts = stopTasks = stopAttempts = 0;
    probeCalls = beginCalls = 0;
    transferCompletes = stopCompletes = beginSucceeds = true;
    eventError = false;
    immediateError = stoppedError = 0;
    probeResult = observedAddress = observedSda = observedScl = 0;
    now = 10;
}

static void expect_empty_probe() {
    const uint8_t outcomes[] = {0, 2, 4};
    for (uint8_t status : outcomes) {
        reset();
        TwoWire wire;
        probeResult = status;
        assert(wire.endTransmission(true) == status);
        assert(probeCalls == 1 && registerWrites == 0);
        assert(txStarts == 0 && rxStarts == 0 && stopTasks == 0);
        assert(observedAddress == 0x40 && observedSda == 4 && observedScl == 5);
        assert(wire._lastActivityUs == now && now > 10);
        assert(!wire._pendingRepeatedStart && wire._txBufferLength == 0);
    }
    reset();
    TwoWire wire;
    assert(wire.endTransmission(false) == 4);
    assert(probeCalls == 0 && registerWrites == 0);
    assert(!wire._pendingRepeatedStart && wire._lastActivityUs == now);
}

static void expect_repeated_start_probe_recovery() {
    for (bool sendStop : {false, true}) {
        for (bool firstStopCompletes : {false, true}) {
            reset();
            TwoWire wire;
            wire._pendingRepeatedStart = true;
            stopCompletes = firstStopCompletes;
            assert(wire.endTransmission(sendStop) == 4);
            assert(wire._pendingRepeatedStart == !firstStopCompletes);
            assert(stopTasks == 1 && probeCalls == 0);
            assert(txStarts == 0 && rxStarts == 0);
            if (!firstStopCompletes) {
                // A second failed STOP must not clear ownership uncertainty.
                assert(wire.endTransmission(true) == 4);
                assert(wire._pendingRepeatedStart && stopTasks == 2);
                assert(probeCalls == 0);
                stopCompletes = true;
                assert(wire.endTransmission(true) == 4);
                assert(!wire._pendingRepeatedStart && stopTasks == 3);
                assert(probeCalls == 0);
            }
            assert(wire.endTransmission(true) == 0);
            assert(probeCalls == 1 && !wire._pendingRepeatedStart);
        }
    }
}

static void expect_write_states() {
    for (bool sendStop : {false, true}) {
        reset();
        TwoWire wire;
        wire._pendingRepeatedStart = true;
        wire._txBufferLength = 2;
        assert(wire.endTransmission(sendStop) == 0);
        assert(wire._pendingRepeatedStart == !sendStop);
        assert(txStarts == 1 && rxStarts == 0 && probeCalls == 0);
        assert(stopTasks == 0 && stopAttempts == (sendStop ? 1U : 0U));
        assert(values[T_DMA_TX_MAXCNT / 4] == 2);
        assert(values[T_DMA_TX_PTR / 4] ==
               static_cast<uint32_t>(reinterpret_cast<uintptr_t>(wire._txBuffer)));
        assert(values[T_ADDRESS / 4] == 0x40);
        assert(wire._txBufferLength == 0 && wire._lastActivityUs == now);
        assert(values[T_SHORTS / 4] == 0);
    }
    for (bool complete : {false, true}) {
        reset();
        TwoWire wire;
        wire._txBufferLength = 1;
        transferCompletes = complete;
        stopCompletes = false;
        assert(wire.endTransmission(true) == 4);
        assert(wire._pendingRepeatedStart && wire._txBufferLength == 0);
        assert(probeCalls == 0 && txStarts == 1 && rxStarts == 0);
        assert(values[T_SHORTS / 4] == 0);
        // The timeout must block an immediate empty GPIO probe.
        assert(wire.endTransmission(true) == 4);
        assert(wire._pendingRepeatedStart && probeCalls == 0);
        stopCompletes = true;
        assert(wire.endTransmission(true) == 4);
        assert(!wire._pendingRepeatedStart && probeCalls == 0);
        assert(wire.endTransmission(true) == 0 && probeCalls == 1);
    }
}

static void expect_write_failures() {
    const uint32_t errors[] = {0, T_TWIM_ERRORSRC_ANACK, T_TWIM_ERRORSRC_DNACK};
    for (bool sendStop : {false, true}) {
        for (bool stopOk : {false, true}) {
            for (uint32_t error : errors) {
                for (bool errorOnly : {false, true}) {
                    reset();
                    TwoWire wire;
                    wire._txBufferLength = 3;
                    transferCompletes = false;
                    immediateError = error;
                    eventError = errorOnly;
                    stopCompletes = stopOk;
                    const uint8_t expected = error == T_TWIM_ERRORSRC_ANACK ? 2 :
                                             error == T_TWIM_ERRORSRC_DNACK ? 3 : 4;
                    assert(wire.endTransmission(sendStop) == expected);
                    assert(wire._pendingRepeatedStart == !stopOk);
                    assert(wire._txBufferLength == 0 && probeCalls == 0);
                    assert(stopTasks == 1 && txStarts == 1 && rxStarts == 0);
                    assert(values[T_TWIM_ERRORSRC / 4] == 0);
                    assert(values[T_SHORTS / 4] == 0);
                }
            }
        }
    }
    reset();
    TwoWire wire;
    wire._txBufferLength = 1;
    stoppedError = T_TWIM_ERRORSRC_DNACK;
    assert(wire.endTransmission(true) == 3);
    assert(!wire._pendingRepeatedStart && probeCalls == 0);
    assert(values[T_TWIM_ERRORSRC / 4] == 0);
}

static void seed_receive_state(TwoWire& wire) {
    wire._rxBufferLength = 7;
    wire._rxBufferIndex = 2;
    wire._peek = 0x33;
}

static void expect_read_states() {
    for (bool sendStop : {false, true}) {
        reset();
        TwoWire wire;
        wire._pendingRepeatedStart = true;
        seed_receive_state(wire);
        assert(wire.requestFrom(uint8_t(0xC0), size_t(3), sendStop) == 3);
        assert(wire._pendingRepeatedStart == !sendStop);
        assert(wire._rxBufferLength == 3 && wire._rxBufferIndex == 0 && wire._peek == -1);
        assert(stopTasks == (sendStop ? 1U : 0U));
        assert(rxStarts == 1 && txStarts == 0 && probeCalls == 0);
        assert(values[T_DMA_RX_MAXCNT / 4] == 3 && values[T_ADDRESS / 4] == 0x40);
        assert(values[T_DMA_RX_PTR / 4] ==
               static_cast<uint32_t>(reinterpret_cast<uintptr_t>(wire._rxBuffer)));
        assert(wire._lastActivityUs == now);
    }
    reset();
    TwoWire wire;
    assert(wire.requestFrom(uint8_t(0x40), size_t(BUFFER_LENGTH + 20), true) == BUFFER_LENGTH);
    assert(values[T_DMA_RX_MAXCNT / 4] == BUFFER_LENGTH && probeCalls == 0);
}

static void expect_read_failures() {
    const uint32_t errors[] = {0, T_TWIM_ERRORSRC_ANACK, T_TWIM_ERRORSRC_DNACK};
    for (bool sendStop : {false, true}) {
        for (bool stopOk : {false, true}) {
            for (bool complete : {false, true}) {
                for (uint32_t error : errors) {
                    for (bool errorOnly : {false, true}) {
                        if (complete && stopOk && error == 0 && !errorOnly) continue;
                        if (complete && !sendStop && error == 0 && !errorOnly) continue;
                        reset();
                        TwoWire wire;
                        seed_receive_state(wire);
                        transferCompletes = complete;
                        immediateError = error;
                        eventError = errorOnly;
                        stopCompletes = stopOk;
                        assert(wire.requestFrom(uint8_t(0x40), size_t(3), sendStop) == 0);
                        assert(wire._pendingRepeatedStart == !stopOk);
                        assert(wire._rxBufferLength == 0 && wire._rxBufferIndex == 0 &&
                               wire._peek == -1);
                        assert(stopTasks == 1 && probeCalls == 0);
                        assert(rxStarts == 1 && txStarts == 0);
                        assert(values[T_TWIM_ERRORSRC / 4] == 0);
                        if (!stopOk) {
                            assert(wire.endTransmission(true) == 4);
                            assert(wire._pendingRepeatedStart && probeCalls == 0);
                            stopCompletes = true;
                            assert(wire.endTransmission(true) == 4);
                            assert(!wire._pendingRepeatedStart && probeCalls == 0);
                            assert(wire.endTransmission(true) == 0 && probeCalls == 1);
                        }
                    }
                }
            }
        }
    }
}

int main() {
    expect_empty_probe();
    expect_repeated_start_probe_recovery();
    expect_write_states();
    expect_write_failures();
    expect_read_states();
    expect_read_failures();
    std::puts("PASS production Wire probe dispatch, payload I/O, and STOP recovery");
}
"""


def compile_and_run(core: str) -> None:
    source = (CORES / core / "Wire.cpp").read_text()
    header = (CORES / core / "Wire.h").read_text()
    helpers = "\n\n".join(
        extract_function(source, signature)
        for signature in (
            "static bool wait_event(",
            "static bool wait_event_or_error(",
            "static uint8_t end_tx_error_code(",
        )
    )
    methods = "\n\n".join(
        extract_function(source, signature)
        for signature in (
            "uint8_t TwoWire::endTransmission(bool sendStop)",
            "uint8_t TwoWire::requestFrom(uint8_t address, size_t quantity, bool sendStop)",
        )
    )
    definitions = dict(
        re.findall(r"(static constexpr uint32_t\s+(T_\w+)\s*=[^;]+;)", source)
    )
    # Select production definitions used by either extracted functions or the
    # harness; the register layout must not silently drift from the core.
    needed = set(re.findall(r"\bT_\w+\b", helpers + methods + HARNESS))
    constants = []
    for definition, name in definitions.items():
        if name in needed:
            constants.append(definition)
            needed.remove(name)
    assert not needed, f"missing production register constants: {needed}"
    buffer_length = re.search(r"^#define BUFFER_LENGTH \d+", header, re.MULTILINE)
    assert buffer_length is not None, "missing production Wire buffer length"
    harness = (
        HARNESS.replace("@CONSTANTS@", "\n".join(constants))
        .replace("@BUFFER_LENGTH@", buffer_length.group(0))
        .replace("@HELPERS@", helpers)
        .replace("@METHODS@", methods)
    )
    compiler = shlex.split(os.environ.get("CXX", "c++"))
    with tempfile.TemporaryDirectory(prefix=f"wire-state-{core}-") as directory:
        cpp = Path(directory) / "wire_state.cpp"
        binary = Path(directory) / "wire_state"
        cpp.write_text(harness)
        subprocess.run(
            compiler + ["-std=c++17", "-Wall", "-Wextra", "-Werror", "-O2",
                        str(cpp), "-o", str(binary)],
            check=True,
        )
        subprocess.run([str(binary)], check=True)
    print(f"PASS {core} executable Wire controller state integration")


def main() -> None:
    for core in ("nrf54l15", "nrf54lm20b"):
        compile_and_run(core)


if __name__ == "__main__":
    main()
