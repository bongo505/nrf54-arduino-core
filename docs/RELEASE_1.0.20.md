# nRF54 Arduino Core 1.0.20

This patch corrects I2C sensor detection reported in
[issue #115](https://github.com/lolren/nrf54-arduino-core/issues/115).

## Changes

- Empty `Wire.endTransmission()` calls now send an address-only **write**,
  rather than a one-byte read. This should allow command-first sensors such as
  the HTU21D to initialize through Adafruit BusIO without a sketch workaround.
- The address-only probe uses open-drain GPIO with bounded clock stretching
  and restores pin and controller state afterward. It sends no dummy payload
  and avoids unsupported zero-length TWIM DMA. Normal payload reads, writes,
  and write/read repeated starts remain on hardware TWIM.
- A failed hardware STOP retains pending-transaction state, preventing an
  empty probe from taking over a controller that may still be active.
- Lower-level `Twim::write()` rejects null buffers and zero-length or oversized
  DMA transfers before accessing hardware.
- Added executable waveform, controller-state, and DMA-validation regression
  tests to CI and release checks for both L15 and LM20A.

Empty `endTransmission(false)` remains unsupported and returns error `4`.
An empty probe following a pending hardware transaction also attempts STOP and
returns `4`, rather than silently splitting a combined transaction.

## Validation

Host tests execute the production probe against a simulated open-drain bus,
covering address bits, ACK/NACK, clock stretching, timing quantization, timeouts,
and GPIO restoration. Controller tests cover payload transfers, repeated-start
state, STOP failures, and recovery. Lower-level TWIM validation tests also pass.

The unmodified Adafruit `HTU21DFtest` example (library 1.1.2, BusIO 1.17.4),
`WireScanner`, and `WireRepeatedStartProbe` compile for both XIAO nRF54L15 and
XIAO nRF54LM20A. The scanner and repeated-start examples are also checked from
the exact release archive.

No debug probe or HTU21D was connected during this work. Physical sensor and
logic-analyzer validation remain outstanding; issue #115 stays open for retest.
See the [implementation and test notes](ISSUE_115_WIRE_ADDRESS_PROBES.md).

## Install

```bash
arduino-cli core update-index
arduino-cli core install "nrf54l15clean:nrf54l15clean@1.0.20"
```

[Changes since v1.0.19](https://github.com/lolren/nrf54-arduino-core/compare/v1.0.19...v1.0.20)
