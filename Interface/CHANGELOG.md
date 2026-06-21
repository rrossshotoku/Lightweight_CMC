# Interface Contract — Change Log

This is the **authoritative change history** for the shared inter-MCU boundary contract
(`mc_if_protocol.h`, `mc_if_od.h`, `INTERFACE_SPEC.md`, `NETWORK_UDP_SPEC.md`).

> **Governance:** the files in this folder are a FROZEN CONTRACT shared by the motor-control
> MCU, the network MCU, and the PC tool. **Any change here must be (1) announced explicitly to
> the user and (2) recorded as an entry below**, so every consumer can update in lockstep. A
> change to the **wire format or OD layout** also bumps `MC_IF_PROTOCOL_VERSION` in
> `mc_if_protocol.h`. Each entry states what changed, the version impact, and **which
> consumers must update**.

## Format
```
## [version] - YYYY-MM-DD  (wire-breaking? yes/no)
### Changed / Added / Removed
- ...
### Consumers to update
- motor-control MCU: ...
- network MCU: ...
- PC tool: ...
```

---

## [1.0.1] - 2026-06-21  (wire-breaking? no)
Operational defaults fixed (agreement-only constants; not part of the byte layout, so no
`MC_IF_PROTOCOL_VERSION` bump).

### Added
- `MC_IF_CYCLIC_RATE_HZ` = 1000, `MC_IF_CYCLIC_PERIOD_US`, `MC_IF_COMMAND_TIMEOUT_MS` = 30,
  `MC_IF_SPI_CLOCK_HZ_INITIAL` = 6 MHz, `MC_IF_SPI_CLOCK_HZ_MAX` = 10 MHz. (`mc_if_protocol.h`)

### Consumers to update
- motor-control MCU: use `MC_IF_COMMAND_TIMEOUT_MS` for the command dead-man; SPI clock per above.
- network MCU: drive the cyclic exchange at `MC_IF_CYCLIC_RATE_HZ`; SPI master clock per above.
- PC tool: no change.

### Resolved (were "still open")
- Cyclic rate, command-timeout, SPI clock now fixed (above). Remaining open: CiA-402 scale
  factors finalisation, UDP ports / endpoint discovery, default telemetry map contents.

---

## [1.0.0] - 2026-06-21  (baseline)
Initial contract.

### Added
- SPI framing: fixed 64-byte full-duplex frame, CRC16/Modbus (header + payload), sync 0xA55A,
  message types (cyclic cmd/status, OD read/write req+resp, heartbeat, error). (`mc_if_protocol.h`)
- OD model: type/access enums, CiA-402 scale factors, control/status/mode constants, and the
  canonical `MC_IF_OD_OBJECTS(X)` map (CiA-402 scaled-int + 0x2xxx manufacturer float32 SI).
  (`mc_if_od.h`)
- Configurable runtime telemetry mapping (TX-PDO) at OD `0x2A00`; cyclic telemetry frame =
  12-byte header + mapped blob (~10 float32). (`mc_if_od.h`, `mc_if_protocol.h`, ADR-014)
- Continuous motion commands (jog/joystick) ride the cyclic command with a `command_counter`
  dead-man watchdog; recommended 1 kHz cyclic / ~30 ms timeout. (`INTERFACE_SPEC.md` §3a)
- Network side: all-UDP PC↔network-MCU protocol — OD access (req/resp, retransmit) + pushed
  telemetry stream (batched, fire-and-forget). (`NETWORK_UDP_SPEC.md`, ADR-014)

### Consumers to update
- motor-control MCU (`Generic_motor_controller`): implement OD + SPI-slave per this contract.
- network MCU (`Lightweight_CMC`): implement SPI master + UDP bridge per this contract.
- PC tool: speak OD over UDP per `NETWORK_UDP_SPEC.md`.

### Still open (not yet frozen; will appear as entries when decided)
- CiA-402 scale factor values, cyclic rate / command-timeout finals, SPI clock ceiling.
- UDP ports / endpoint discovery; default telemetry map contents.
