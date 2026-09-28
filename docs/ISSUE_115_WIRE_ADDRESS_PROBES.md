# Wire address-only writes (issue #115)

## Problem

An empty `Wire.beginTransmission(address)` / `Wire.endTransmission()` pair
must test the target's **write** address without sending a register or payload.
Adafruit BusIO uses this sequence during sensor initialization. The HTU21D
reporter found that its write address was acknowledged but its read address was
not acknowledged before a command had been sent.

The core previously substituted a one-byte read for empty writes. This avoided
an older nRF54 zero-length DMA scanner problem, but changed the transaction's
direction and could consume device data. Consequently, `htu.begin()` could fail
even though temperature and humidity measurements worked when initialization's
return value was ignored.

References:

- [Issue #115 and the reporter's reproduction](https://github.com/lolren/nrf54-arduino-core/issues/115)
- [Adafruit BusIO address detection](https://github.com/adafruit/Adafruit_BusIO/blob/master/Adafruit_I2CDevice.cpp)
- [Nordic nRF54L15 TWIM registers](https://docs.nordicsemi.com/r/bundle/ps_nrf54l15/page/twim.html-topic), `DMA.TX.MAXCNT`: valid nonzero transfer counts.
- Nordic *nRF54LM20A/nRF54LM20B Datasheet v1.0*, section 8.24.10.39,
  page 721: `DMA.TX.MAXCNT` range is `1..0xFFFF`.

## Implementation

Only the empty, STOP-terminated controller transaction uses GPIO. All payload
writes, reads, and ordinary write/read repeated starts still use hardware TWIM.

The probe temporarily releases TWIM's pins and generates:

```text
START -> seven-bit address -> WRITE (0) -> target ACK/NACK -> STOP
```

For the HTU21D at `0x40`, the address byte on the wire is `0x80`, not `0x81`.
There is no dummy register write, payload byte, or fallback read. No application
or Adafruit library changes are required for ordinary `htu.begin()` detection.

- GPIO is open-drain, with input sampling enabled; neither line is driven high.
- Six-microsecond phase waits allow for `micros()` quantization while keeping
  the probe below 100 kHz and meeting Standard-mode minimum timing. Payload
  transfers retain the speed selected by `Wire.setClock()`.
- SCL release waits for clock stretching, with a 25 ms timeout per wait and
  wrap-safe elapsed-time checks.
- A bus already held low cannot be mistaken for an address ACK.
- GPIO configuration, output latches, and TWIM enable state are restored on
  success, NACK, and timeout. Interrupts remain enabled.
- Return codes are `0` for ACK plus successful STOP, `2` for address NACK,
  and `4` for bus/timeout/unsupported-operation failures.
- Empty `endTransmission(false)` remains unsupported and returns `4`.
  An empty probe following a held hardware transaction also aborts that
  transaction with STOP and returns `4`; it does not silently insert a STOP
  and report a successful combined transaction.
- A hardware STOP timeout retains the pending-transaction state. A later empty
  probe must attempt STOP and report an error before any GPIO takeover.

The lower-level `Twim::write()` API is a payload-only DMA operation. It rejects
zero length, null buffers, and oversized transfers before accessing hardware;
use `Wire` for address-only probing.

## Validation

`scripts/test_wire_address_probe_contracts.py` executes the production GPIO
helper against a simulated open-drain bus for both core implementations. It
checks transmitted addresses and direction, ACK/NACK, STOP, stretching, faults,
and restoration. `scripts/test_wire_controller_state.py` executes controller API
paths with fault-injected hardware events, including pending-transaction
recovery. `scripts/test_twim_write_validation.py` exercises lower-level DMA
validation. All three tests run in CI and the release workflow.

Focused compile checks cover the unmodified Adafruit `HTU21DFtest` example
(HTU21DF library 1.1.2, BusIO 1.17.4), `WireScanner`, and
`WireRepeatedStartProbe` on XIAO nRF54L15 and XIAO nRF54LM20A.

Hardware acceptance remains required: run the unmodified Adafruit
[`HTU21DFtest` example](https://github.com/adafruit/Adafruit_HTU21DF_Library/blob/master/examples/HTU21DFtest/HTU21DFtest.ino)
with an HTU21D, verify `htu.begin()` succeeds and readings
continue, then verify `WireScanner` detects connected devices without reporting
unconnected addresses. A logic-analyzer capture should show the write address,
ACK, and STOP with no payload. Test `Wire1` as well where available.

No connected debug probe or HTU21D was available during this implementation;
host tests and compilation do not establish electrical or sensor validation.
