# app/cia402

> **The wire format and the OD entry list are owned by `Interface/`.** This module *consumes* `Interface/mc_if_protocol.h` and `Interface/mc_if_od.h`. It does not redefine them. Any change to the contract is an Interface change (bumps `MC_IF_PROTOCOL_VERSION`) and must be flagged before editing.

## Purpose
CMC-side counterpart of the CiA-402 state machine that lives on the motor MCU, and the codec for the two SPI message families this CMC sends:
1. **Cyclic command / status** (`CYCLIC_CMD` ↔ `CYCLIC_STATUS`) — the always-on real-time channel, encoded per `MC_IfCyclicCommand_t` / `MC_IfCyclicStatusHeader_t` (+ telemetry blob).
2. **OD read / write transactions** (`OD_READ_REQ` / `OD_WRITE_REQ` and their responses) — pipelined onto the same SPI transactions as the cyclic channel.

This is **not** standard CANopen SDO bytes on the wire — the Interface uses a custom 64-byte full-duplex framing with `sync 0xA55A`, CRC-16/Modbus on header and payload, and a `message_type` field that selects between the cyclic and OD-access payloads.

## Owns
- The host-side view of the CiA-402 state machine: track the motor's reported `MC_IfNodeState_t` and walk the drive to `OPERATION_ENABLED` (or the configured target) via `controlword` writes inside `CYCLIC_CMD`.
- The codec for `MC_IfFrameHeader_t` + `MC_IfFrameFooter_t`: build outbound, validate inbound, compute CRC-16/Modbus.
- The dispatcher that decides whether the next SPI transaction carries `CYCLIC_CMD` or an `OD_READ_REQ` / `OD_WRITE_REQ` (OD requests are pipelined; the cyclic channel continues otherwise).
- Sequence-number management for OD request/response correlation.
- The mapping from `MC_IfOdResult_t` abort codes into our internal status enum.

## Does NOT do
- Decide motor policy (which mode, what velocity, when to enable). That's `motor_ctrl`.
- Talk SPI hardware. Calls `bsp/motor_spi` for the transfers.
- Define OD entries, scale factors, or the OD layout. Pulls those verbatim from `Interface/mc_if_od.h`.

## Public API
```c
void   cia402_init(void);
void   cia402_tick(void);    /* runs the cyclic exchange + services OD pipelined work */

/* Drive state machine */
MC_IfNodeState_t cia402_get_node_state(void);
cia402_status_t  cia402_request_state (MC_IfNodeState_t target);

/* Compose the next CYCLIC_CMD. motor_ctrl populates these, cia402 packs them. */
void cia402_set_mode      (int8_t   mode_of_operation);   /* OD 0x6060 */
void cia402_set_controlword(uint16_t cw);                 /* OD 0x6040 */
void cia402_set_target_velocity(int32_t v);               /* OD 0x60FF (scaled per mc_if_od.h) */
void cia402_set_target_position(int32_t p);               /* OD 0x607A */
void cia402_set_target_torque  (int32_t t);               /* OD 0x6071 */
void cia402_set_profile        (uint32_t pv, uint32_t pa, uint32_t pd);

/* Read back the latest CYCLIC_STATUS. */
const MC_IfCyclicStatusHeader_t * cia402_latest_status(void);

/* OD access. Pipelined — request is sent on the next transaction, response
 * arrives on a later one. Caller polls via the future-style API below. */
typedef int16_t cia402_od_request_t;             /* >=0: pending handle; <0: error */

cia402_od_request_t cia402_od_read_begin (uint16_t idx, uint8_t sub, MC_IfOdType_t type);
cia402_od_request_t cia402_od_write_begin(uint16_t idx, uint8_t sub, MC_IfOdType_t type,
                                          const void *data, uint8_t len);
bool                cia402_od_poll       (cia402_od_request_t h,
                                          MC_IfOdResult_t *out_result,
                                          void *out_data, uint8_t *out_len);
```

## Dependencies
- `Interface/mc_if_protocol.h`, `Interface/mc_if_od.h` (vendored under the project as a frozen contract).
- `bsp/motor_spi` (one full-duplex 64-byte exchange per `cia402_tick`).
- `bsp/time` (cyclic cadence, OD-request timeouts).

## Acceptance criteria
- `cia402_tick` produces one valid `CYCLIC_CMD` per call (header + footer CRCs correct, payload matches the configured fields).
- Round-trip: write target velocity via `cia402_set_target_velocity` → observed in next `CYCLIC_STATUS` actuals after one cycle of motor processing.
- `cia402_request_state(MC_IF_NODE_RUNNING)` walks the drive through the CiA-402 transitions and returns success within a bounded number of cycles, or a meaningful failure status.
- `cia402_od_read_begin` followed by `cia402_od_poll` returns the value the motor MCU has for that OD index, or `MC_IfOdResult_t != MC_IF_OD_OK` on access failure.
- Bad-CRC / bad-version / bad-length response frames are rejected; an `ERROR` is logged; the cyclic channel keeps running.

## Notes
- The cyclic cadence is set by `bsp/motor_spi` / `motor_ctrl` policy (default ~1 kHz per the Interface spec).
- `command_counter` in `CYCLIC_CMD` increments each cycle. Slave checks for stale-command timeout — see `INTERFACE_SPEC.md §3` "Timeout / safe-state".
- The telemetry blob inside `CYCLIC_STATUS` is consumed by `app/od` (for the network telemetry stream) — not by this module. `cia402` exposes the *header* of the cyclic status and the raw blob bytes; OD interprets them using the active `map_version`.
