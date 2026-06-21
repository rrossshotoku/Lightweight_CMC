# Lightweight CMC — Architecture

## ⚠ Maintenance rule

**This document is the contract for how the Lightweight CMC is built.** Any change to module boundaries, public APIs, hardware allocation, protocol behaviour, persistent storage, port numbers, or task ordering MUST update this document **in the same change that touches the code**. The implementation is allowed to follow this document; this document is not allowed to drift from the implementation.

See `Documentation/README.md` for the wider documentation upkeep rule and how the documents relate to module READMEs.

## ⚠ `Interface/` is the cross-project boundary

The repository contains a top-level **`Interface/`** folder that is the **single source of truth** for two interfaces shared with **other codebases** (the motor-control MCU firmware and the PC tool):

- **SPI framing CMC ↔ motor MCU** — `Interface/mc_if_protocol.h` + `Interface/INTERFACE_SPEC.md`.
- **UDP framing PC ↔ CMC for OD access and telemetry** — same headers + `Interface/NETWORK_UDP_SPEC.md`.
- **Object Dictionary layout** (CiA-402 + manufacturer entries) — `Interface/mc_if_od.h`.

This document **defers to `Interface/`** for any of the above. Where this file restates a value (port number, framing field, OD index, etc.), the restatement is for readability; the Interface file wins on disagreement.

**Changes to `Interface/`** affect at least three codebases, so they are deliberate: any edit bumps `MC_IF_PROTOCOL_VERSION` (see top of `Interface/INTERFACE_SPEC.md`) and must be flagged so the other consumers can be rebuilt against the same version.

---

## 1. Introduction

### Purpose

The Lightweight CMC (Camera Motion Controller) is a single-axis camera-motion node, intended as a smaller, simpler member of the Shotoku CMC family. It speaks the CAMERAD protocol to standard Shotoku S-type and T-type controller panels, controls a single motor via a separate Motor Controller MCU over SPI, and exposes a small set of remote interfaces over a single Ethernet port.

### Scope — in

- **CAMERAD** server for S-type and T-type panels: poll, select/deselect/grab, key-presses (type 1/2/3), position request, movement, joystick profile.
- **CiA 402 Profile Velocity** motor control over SPI to a separate motor MCU. Trajectory planning for shot recall (CMC-side; the motor MCU does PV only).
- **Object Dictionary** shared between this CMC and the motor MCU. Exposed on the network as CANopen **SDO-over-UDP** (8-byte expedited SDO in the UDP payload).
- **Web UI** for network configuration, motor soft-limit setting, status display, and OD browsing. HTTP Basic authentication.
- **Diagnostic logging** via a TCP socket (no UART, no SWO).
- **Persistent storage** of network configuration, motor limits, authentication credentials, and ~100 shot positions in internal flash with power-fail-safe dual-bank saves.

### Scope — out

- Full CANopen master/slave stack on a CAN bus.
- CiA 402 Profile Position and Cyclic Synchronous Position modes.
- CAMERAD message families outside the S/T panel set (CCU, NHM, on-air broadcasts, zone info, cue computer integration).
- Multi-axis motion. The CMC is single-axis today; the module structure can extend to more axes by adding entries to the OD and the limits table, but the wiring for >1 SPI motor MCU is not in scope.
- Any data persistence the user cannot reproduce from the front panel or web.
- Wall-clock time (no RTC).

---

## 2. Hardware

| Item | Choice |
|---|---|
| MCU | STM32G431RBTx on Nucleo-G431RB |
| Core / clock | Cortex-M4F @ 170 MHz from HSI × PLL |
| Flash | 128 KB internal, 2 KB sectors |
| Ethernet | WIZnet W6100 on SPI2 (hardware TCP/IP, 8 sockets) |
| Motor link | SPI3 master → separate Motor Controller MCU |
| I2C2 | Placeholder, no device |
| Diagnostic | TCP log socket on the same Ethernet port — no UART, no SWO |
| User I/O | Nucleo green LED (heartbeat), user push-button (unused in this revision) |
| Power | Nucleo USB / external 5V — out of scope here |

Flash budget (rough):

| Region | Bytes | Notes |
|---|---:|---|
| Vector + code  | up to ~80 KB | Compile-time |
| `config` (dual-bank) | 2 × 2 KB | One general settings region |
| `shots`  (dual-bank) | 2 × 4 KB | 100 shot entries × axes × 4 bytes, comfortably under one sector — dual-bank doubles |
| Reserved (boot, future use) | balance | — |

---

## 3. System context

```
                         ┌───────────────────────────────┐
                         │  S-type or T-type panel       │
                         │  (multiple, up to 3 connected)│
                         └─────────────┬─────────────────┘
                                       │ CAMERAD over UDP+TCP
                                       │
   ┌───────────────────┐               │              ┌──────────────────────┐
   │  Browser          │── HTTP ───────┤              │  OD client / CLI     │
   │  (config + limits)│   (port 80)   │              │  (Python tool)       │
   └───────────────────┘               │              └─────────┬────────────┘
                                       │                        │ SDO-over-UDP
                                       │                        │ (port 30100)
                                       ▼                        │
                         ┌──────────────────────────────────────┴───────┐
                         │           Lightweight CMC (this device)      │
                         │                                              │
                         │  ┌────────────┐   ┌────────────┐             │
                         │  │  W6100     │   │  STM32G431 │             │
                         │  │  Ethernet  │◄──┤            │             │
                         │  └────────────┘   │            │             │
                         │                   │            │             │
                         │  ┌────────────┐   │            │             │
                         │  │  Internal  │◄──┤            │             │
                         │  │   flash    │   │            │             │
                         │  └────────────┘   │            │             │
                         │                   └─────┬──────┘             │
                         └─────────────────────────┼────────────────────┘
                                                   │ SPI3 + CiA 402
                                                   │ (Profile Velocity)
                                                   ▼
                         ┌───────────────────────────────────────────────┐
                         │      Motor Controller MCU (separate board)    │
                         │      Drives the actual motor                  │
                         └───────────────────────────────────────────────┘
                                                   │
                                                   ▼
                                            ┌─────────────┐
                                            │   Motor     │
                                            └─────────────┘

                         ┌──────────────────┐
                         │  Network log     │
                         │  consumer        │
                         │  (`nc` etc.)     │
                         └────────┬─────────┘
                                  │ TCP log
                                  │ (port 30200)
                                  ▼
                            (one connected client at a time)
```

Five external interfaces, all over the same Ethernet port:

| Interface | Transport | Default port | Purpose |
|---|---|---:|---|
| CAMERAD poll       | UDP        | 30002 | S/T panel discovery |
| CAMERAD control    | TCP        | 30003 | Panel↔CMC commands & responses |
| OD network port    | UDP (SDO)  | 30100 | Read/write OD entries from network tools |
| Web UI             | TCP (HTTP) | 80    | Configuration & status pages |
| Log stream         | TCP        | 30200 | Diagnostic output |

---

## 4. Top-level architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  main_loop  (cooperative event tick — owns init and tick order)    │
└──┬─────────────┬─────────────┬─────────────┬─────────────┬─────────┘
   │             │             │             │             │
   ▼             ▼             ▼             ▼             ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│controller│ │   web    │ │  motor   │ │   od     │ │   log    │
│  _mgr    │ │          │ │  _ctrl   │ │          │ │          │
└────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
     │            │            │            │            │
     ▼            ▼            ▼            ▼            │
┌──────────────────────────────────────────────────┐    │
│              cmc_state        config              │    │
│        (selection, status,   (settings,           │    │
│         shots, profile)       limits, persist)    │    │
└──────────────┬───────────────────┬───────────────┘    │
               │                   │                    │
               ▼                   │                    │
        ┌──────────┐               │                    │
        │ camerad  │   ┌──────────┐│                    │
        │ (codec)  │   │  cia402  ││                    │
        └──────────┘   │ (sm +    ││                    │
                       │  codec)  ││                    │
                       └─────┬────┘│                    │
                             │     │                    │
                             ▼     ▼                    ▼
                       ┌────────────────────────────────────┐
                       │  bsp/                              │
                       │  net   motor_spi   flash  time wdg │
                       └────────────────────────────────────┘
                                       │
                                       ▼
                       ┌────────────────────────────────────┐
                       │  Drivers/  (CubeMX HAL + w6100)    │
                       └────────────────────────────────────┘
```

No RTOS. The system is a cooperative super-loop driven by `main_loop`, which calls each top-level module's `_tick` once per pass and kicks the watchdog. There are no application threads; long-running operations (e.g. trajectory streaming) are decomposed into per-tick state machines.

---

## 5. Layering rules

1. Dependencies flow **downward only**. A higher-layer module may include a lower-layer header; the reverse is forbidden.
2. Layers, top to bottom:
   1. `app/main_loop`
   2. `app/controller_mgr`, `app/web`, `app/motor_ctrl`, `app/od`, `app/log`
   3. `app/cmc_state`, `app/config`
   4. `app/camerad`, `app/cia402`
   5. `bsp/*`
   6. `Drivers/*` (CubeMX HAL + WIZnet ioLibrary)
3. No file in `app/` includes anything from `Core/` or `Drivers/` directly. App code uses `bsp/` headers only.
4. No file in `bsp/` includes anything from `app/`.
5. `cmc_state` is the only module that holds runtime CMC state. Every other module reads/mutates via its API; no global fields are exposed.
6. `camerad` and `cia402` are pure codecs: parse bytes → struct, build struct → bytes. They hold no state.
7. **Soft file-size cap ~400 lines, function ~100 lines.** When a file approaches the limit, split before exceeding it.

---

## 6. Module catalogue

| Layer | Module | Path | Responsibility | README |
|---|---|---|---|---|
| L1 | `main_loop`      | `app/main_loop/`      | Init order; tick loop; watchdog kick | `app/main_loop/README.md` |
| L2 | `controller_mgr` | `app/controller_mgr/` | UDP poll listen; per-controller TCP; dispatch | `app/controller_mgr/README.md` |
| L2 | `web`            | `app/web/`            | HTTP/1.0 server; pages; JSON API; HTTP Basic auth | `app/web/README.md` |
| L2 | `motor_ctrl`     | `app/motor_ctrl/`     | High-level motor API; trajectory generator | `app/motor_ctrl/README.md` |
| L2 | `od`             | `app/od/`             | OD registry; dispatch; SDO-over-UDP network port | `app/od/README.md` |
| L2 | `log`            | `app/log/`            | RAM ring buffer; TCP log socket | `app/log/README.md` |
| L3 | `cmc_state`      | `app/cmc_state/`      | Selection, status bits, joystick profile, shot table | `app/cmc_state/README.md` |
| L3 | `config`         | `app/config/`         | Versioned settings, motor limits, auth, node ID | `app/config/README.md` |
| L4 | `camerad`        | `app/camerad/`        | CAMERAD wire codec (header + body) | `app/camerad/README.md` |
| L4 | `cia402`         | `app/cia402/`         | CiA 402 state machine + SDO codec over SPI | `app/cia402/README.md` |
| L5 | `bsp/net`        | `bsp/net/`            | Socket API over W6100 | `bsp/net/README.md` |
| L5 | `bsp/motor_spi`  | `bsp/motor_spi/`      | Framed SPI transfer to motor MCU | `bsp/motor_spi/README.md` |
| L5 | `bsp/flash`      | `bsp/flash/`          | Internal flash erase/program; dual-bank picker | `bsp/flash/README.md` |
| L5 | `bsp/time`       | `bsp/time/`           | Monotonic ms tick | `bsp/time/README.md` |
| L5 | `bsp/wdg`        | `bsp/wdg/`            | IWDG init + kick | `bsp/wdg/README.md` |
| L5 | `bsp/identity`   | `bsp/identity/`       | Stable per-unit identity (MAC etc.); source-swappable | `bsp/identity/README.md` |
| L6 | `Drivers/STM32G4xx_HAL_Driver` | `Drivers/` | CubeMX HAL — untouched | — |
| L6 | `Drivers/BSP/STM32G4xx_Nucleo` | `Drivers/` | Nucleo BSP (LED, button) — untouched | — |
| L6 | `Drivers/w6100`  | `Drivers/w6100/`      | WIZnet ioLibrary (vendor, dropped in for Phase 0b) | — |

---

## 7. Cross-cutting concerns

### 7.1 Watchdog

- IWDG initialised in `main_loop_init`, before any other init runs.
- Default timeout: **250 ms**.
- `wdg_kick()` is called exactly once per `main_loop_run` pass, from `main_loop` itself. No other module may kick.
- Debugger-freeze enabled (`__HAL_DBGMCU_FREEZE_IWDG`) so breakpoints do not cause spurious resets.
- A `wdg_force_reset()` path exists for deliberate resets (e.g. from `Error_Handler` or a fatal protocol fault). It disables interrupts and busy-waits for IWDG expiry.

### 7.2 Time

- `bsp/time` is the only time source the application uses. No `HAL_GetTick()` calls outside `bsp/time`.
- No `HAL_Delay()`. Anywhere. Non-blocking only.
- `time_elapsed_ms()` and `time_after()` are wrap-safe.

### 7.3 Errors

- All public functions return a status code; void is reserved for "cannot fail" or "fire-and-forget".
- Fatal errors set a flag in `cmc_state` and let the next watchdog tick reset. **No `while(1)` in error paths.**
- `__disable_irq()` is forbidden outside the deliberate `wdg_force_reset()` path.

### 7.4 Wire formats

- **CAMERAD** (CMC ↔ S/T panels): integer counts only, little-endian, `#pragma pack(1)`. No CRC; relies on TCP/UDP checksums.
- **CMC ↔ Motor MCU** (SPI): per `Interface/mc_if_protocol.h` — fixed 64-byte full-duplex frame, little-endian, CRC-16/Modbus on header and payload separately. CiA-402 standard objects are scaled integers; manufacturer (`0x2xxx`) objects are float32 SI (this is the one exception to the "no floats on the wire" rule and is deliberate — it's the tuning/telemetry path).
- **PC ↔ CMC OD** (UDP): per `Interface/NETWORK_UDP_SPEC.md` — 8-byte `MC_UdpHeader_t` + typed payload, little-endian. UDP checksum only.
- **HTTP / log**: ASCII / UTF-8 over TCP. No CRC.

### 7.5 Memory

- All buffers sized at compile time.
- **No `malloc`/`free`** after `main_loop_init` returns.
- Stack budget per task is one stack (single-thread cooperative loop). Estimate: ~4 KB main stack, ~256 B per nested call chain. Confirm with linker map.

### 7.6 Flash caches & wait states

- ART accelerator + prefetch + I-cache + D-cache must be enabled in `stm32g4xx_hal_conf.h`. Confirm before first flash run.
- Wait states are `FLASH_LATENCY_4` (correct for 170 MHz, set by CubeMX).

### 7.7 Logging hygiene

- Log lines go through `LOG_{DEBUG,INFO,WARN,ERROR}` only.
- Protocol modules (`camerad`, `cia402`, `controller_mgr`, `od`) never `printf` — they may log codes/IDs, not raw bytes.
- `log_printf` does not support `%f` (nano-newlib has no float-printf path).

### 7.8 Interrupt priorities

- HAL SysTick is the lowest IRQ priority so it never preempts user IRQs that need the kernel.
- W6100 has no NVIC priority of its own (no INT line wired into a fast IRQ for Phase 0b — polled). If interrupt-driven RX is added later, it must respect the kernel-aware boundary.
- SPI3 (motor) DMA priority kept moderate. No NVIC priority below `4` (kernel-aware boundary equivalent on this part is informal but the rule of thumb holds: keep app IRQs in the upper half, leave the bottom half for time-critical hardware that must never be blocked).

---

## 8. Data flows

### 8.1 CAMERAD POLL → response (S/T panel, idle case)

```
S-panel                  CMC                                            W6100
   |                      |                                                |
   |---POLL UDP 30002---->| (bsp/net detects RX on socket 0)               |
   |                      |--ctrl_mgr_tick reads UDP------------>          |
   |                      |--camerad_parse_header(bytes)                    |
   |                      |--cmc_add_or_update_controller(no, addr, port)   |
   |                      |--cmc_get_camera_status(), cmc_get_cmc_status()  |
   |                      |--camerad_build_poll_response_s(body)            |
   |                      |--ensure outbound TCP to panel.return_port--->   |
   |<------poll response (TCP, S-shape body, message_id echoed)----------   |
   |                      |                                                |
```

Variants:
- For a T-panel (`dReturnDevice == dCameraDControllerT`), the body shape is the T variant (no `shot_no`/`next_shot_no`).
- If an inbound TCP connection from the panel is already established (panel-side TCP listener), responses use that socket instead of opening outbound.
- If `cmc_state` reports a CCU mode != 0 *and* this CMC is selected by the polling controller, a CCU status message follows the poll response. **Out of scope for the Lightweight CMC — never sent.**
- A POLL from a controller that has timed out (> 5 s since last poll) is treated as a re-registration; previous TCP socket (if any) is closed first.

### 8.2 Select → confirm → joystick → motor

```
S-panel                  CMC                                Motor MCU
   |                      |                                       |
   |---SELECT (TCP)------>|                                       |
   |                      |--cmc_select(ctrl_no)                  |
   |                      |--build S/T poll-response body         |
   |<--poll-response (camera_selected=1, controller_no=ctrl_no)---|
   |                      |                                       |
   |---MOVEMENT (TCP)---->|                                       |
   |  (every ~25 ms)      |--camerad_parse_movement               |
   |                      |--cmc_state apply joystick profile     |
   |                      |--motor_jog(scaled_velocity)           |
   |                      |--cia402_set_target_velocity()         |
   |                      |--bsp/motor_spi_transfer(SDO_write)---->|
   |                      |<--SDO response (success or abort)-----|
   |                      |                                       |
   |                      |--motor_ctrl_tick reads statusword     |
   |                      |--cia402_get_statusword                |
   |                      |--bsp/motor_spi_transfer(SDO_read)----->|
   |                      |<--statusword (uint16)----------------|
   |                      |--cmc_state.moving / on_shot updated   |
   |                      |                                       |
   |---POLL (UDP)-------->|                                       |
   |<--poll response (camera_status.MOVING=1)---|                 |
```

### 8.3 Shot recall (fade)

```
S-panel                  CMC                                Motor MCU
   |                      |                                       |
   |---KEYPRESS1          |                                       |
   |   (KEY_FADE, shot=N)>|                                       |
   |                      |--cmc_shot_recall(N) -> target counts  |
   |                      |--motor_move_to(target, duration_ms)   |
   |                      |   = trajectory_init(now, here, target,|
   |                      |                     duration)         |
   |                      |--cia402_set_target_velocity(v0)----->|
   |<--KEYPRESS1 response (moving=1, time_to_shot=...)            |
   |                      |                                       |
   |                      |--motor_ctrl_tick (every ~25 ms):      |
   |                      |    trajectory_step() -> next velocity |
   |                      |--cia402_set_target_velocity(v_n)----->|
   |                      |    ...repeated for duration...        |
   |                      |    on arrival: ramp to 0, set on_shot |
   |                      |                                       |
   |---POLL (UDP)-------->|                                       |
   |<--poll response (camera_status.ON_SHOT=1, MOVING=0)----      |
```

Cancellation: any new movement-related command (joystick, new shot, stop) before the trajectory completes calls `trajectory_cancel()` and the streamer either ramps to zero (stop) or starts a new trajectory (new target).

### 8.4 OD read via the network port

```
OD client            CMC                            Motor MCU
   |                  |                                  |
   |--SDO request---->|  (UDP port 30100, 8 bytes)       |
   |                  |--od_tick decodes SDO request     |
   |                  |--od_read(idx, sub)               |
   |                  |    if local entry:               |
   |                  |       call registered handler    |
   |                  |       return value               |
   |                  |    if proxy entry (6000h..67FFh):|
   |                  |       cia402_sdo_read(idx, sub)  |
   |                  |       SPI exchange ────────────>|
   |                  |       <─── value or abort ──────|
   |                  |--od_build_sdo_response(value)    |
   |<--SDO response--|  (UDP, 8 bytes)                   |
```

Errors return standard CANopen abort codes (e.g. `0x06020000` "object does not exist", `0x06010002` "attempt to write a read-only object").

### 8.5 Web config change

```
Browser              CMC
   |                  |
   |--GET /network--->| (TCP socket 6, HTTP)
   |                  |--web_tick parses request
   |                  |--HTTP Basic auth check against config_get_auth()
   |                  |  (default-password check; warning shown if defaulted)
   |                  |--build form HTML with current config_get_network()
   |<--200 OK (HTML)--|
   |                  |
   |--POST /network-->|
   |                  |--HTTP Basic auth check
   |                  |--parse form-encoded body
   |                  |--config_set_network(new_cfg) -> bsp/flash save
   |                  |--respond with "applies on next boot" page
   |<--200 OK (HTML)--|
```

---

## 9. Persistent state

| What | Where | Volatile? | Notes |
|---|---|---|---|
| Network config (MAC, IP, mask, gw, ports, device_no) | `config` (flash, dual-bank region A) | No | Loaded at boot, applied to `bsp/net` on init |
| Motor soft limits (per axis low/high) | `config` (flash, region A) | No | Read by `motor_ctrl` on every motion command |
| Auth credentials (username, hashed password, salt, default-flag) | `config` (flash, region A) | No | Hashed SHA-256 + salt; default-password flag forces a change |
| CANopen node ID | `config` (flash, region A) | No | 1..127 |
| Shot table (~100 entries × axes × int32) | `config` (flash, dual-bank region B) | No | Saved on store; loaded into `cmc_state` at boot |
| Current selection (selected_by) | `cmc_state` RAM only | Yes | Cleared on boot |
| Camera status bits | `cmc_state` RAM only | Yes | Cleared on boot; derived bits (Moving/OnShot) come from `motor_ctrl` |
| Joystick profile (Normal/Medium/Fine) | `cmc_state` RAM only | Yes | Defaults to Normal on boot |
| Time-to-shot, current/next shot | `cmc_state` RAM only | Yes | Set by controller commands |
| Trajectory state (target, profile, progress) | `motor_ctrl` RAM only | Yes | Cleared on boot / cancellation |
| Cached motor statusword, position | `motor_ctrl` RAM only | Yes | Refreshed by `motor_ctrl_tick` |
| Log ring buffer | `log` RAM only | Yes | 4 KB |

Regions A and B occupy separate flash sector pairs so a bad save of one cannot corrupt the other.

---

## 10. Network design

### 10.1 Socket allocation

W6100 has 8 hardware sockets. Compile-time map (see also `bsp/net/README.md`):

| Socket | Type | Port (default) | Owner | Purpose |
|---:|---|---:|---|---|
| 0 | UDP | 30002 | `controller_mgr` | CAMERAD poll listen |
| 1 | TCP listen | 30003 | `controller_mgr` | CAMERAD TCP listen |
| 2 | TCP | dynamic | `controller_mgr` | Per-controller (in or out) |
| 3 | TCP | dynamic | `controller_mgr` | Per-controller (in or out) |
| 4 | UDP | **5000** | `od` | OD access (PC ↔ CMC, see `Interface/NETWORK_UDP_SPEC.md`) |
| 5 | UDP | **5001** | `od` | Telemetry stream (CMC → subscribed PC) |
| 6 | TCP | 30200 | `log` | Log stream (one connected client at a time) |
| 7 | TCP listen | 80 | `web` | HTTP |

Implication: **max 2 simultaneous controllers** (limited by sockets 2/3). The previous plan budgeted 3 controllers; the OD telemetry port displaces one slot. A typical 1×S + 1×T deployment is still covered.

The OD UDP ports (`5000` / `5001`) match `Interface/NETWORK_UDP_SPEC.md` and are user-settable via the web config page. The other port numbers (CAMERAD, log, HTTP) are local to this CMC and not in the Interface contract.

### 10.2 Traffic patterns

- CAMERAD UDP polls: each controller polls at ~1 Hz when idle, faster during movement.
- CAMERAD TCP commands (movement messages especially): up to 40 Hz per selected controller.
- HTTP: human-driven, sporadic, one request at a time.
- SDO-over-UDP: tool-driven, sporadic during commissioning; potentially continuous during a tuning session (rate-limited by `od_tick` cadence, default 100 ms).
- Log TCP: one stream, up to a few KB/s during verbose logging.
- W6100 internal RX buffers: ~2 KB per socket by default. Adequate for the above.

### 10.3 Address handling

- IP/MAC come from `config`. Static. Changing either via the web is a "applies on next boot" operation (not silent rebind during a session).
- DHCP is out of scope.
- Discovery broadcast (a CMC announcing itself on a fixed UDP port) is out of scope. Tools use the configured IP.

---

## 11. Protocol decisions

### 11.1 CAMERAD

- **Authoritative reference**: the Main CMC source (`SW050 ARM Based CMC Processor`), specifically `Trunk/CMCapp/CameraToolsU.h` and `Trunk/Other/NetComms.c`. The "Network Protocol Head Movement" PDF is reference only.
- **Version advertised**: `"1.3"`. Negotiation responsibility lives in `controller_mgr`; this CMC only builds 1.3 response shapes.
- **Strict 8-byte magic check** in `camerad`. (The Reduced CMC's 7-byte check is a bug to avoid.)
- **Response shape (S vs T) is determined by the requesting `device_type`**, never by the inbound key code. (Reduced CMC bug to avoid.)
- **`message_id` echoed** on every response (Main CMC convention).
- **Inbound TCP from a controller is preferred** for sending responses. Outbound (CMC opens TCP to controller's `return_port`) is the fallback. (This matches Main CMC `REDUCE_TCP_SOCK` behaviour; the Reduced CMC's outbound-only approach also works in practice and is acceptable initially, but supporting inbound is on the roadmap.)
- **Joystick profile keys handled as Type-2** (Main CMC convention), and **applied to incoming movement values** via a scaling step in `motor_ctrl`. (The Reduced CMC's Type-1 handling and no-effect storage is a bug to avoid.)
- **Camera status vs CMC status**: Moving and OnShot live in `camera_status` *only*. (The Reduced CMC duplicates them across both words — a bug to avoid.)
- **No CRC, no checksum, no authentication**. CAMERAD relies on TCP/UDP integrity and an assumed-trusted LAN.

The exact subset of opcodes this CMC accepts and what each response carries is `Documentation/camerad_subset.md` (TBD, drafted with Phase 2).

### 11.2 CMC ↔ Motor MCU over SPI

The wire format is defined by `Interface/mc_if_protocol.h` and explained in `Interface/INTERFACE_SPEC.md`. **The summary below is illustrative — Interface is authoritative.**

- **Fixed 64-byte full-duplex frame per SPI transaction.** Header (10 B: sync `0xA55A`, version, message_type, payload_length, sequence, header CRC-16/Modbus) + payload (≤ 52 B) + footer (2 B: payload CRC-16/Modbus) + zero padding to 64.
- **NSS per transaction**, slave DMA armed for exactly 64 bytes; self-synchronising on NSS edge.
- **Two coexisting channels on the same SPI transactions**:
  - **Cyclic** (PDO-equivalent): `CYCLIC_CMD` master→slave + `CYCLIC_STATUS` slave→master, both packed structs. Default cadence ~1 kHz. `CYCLIC_CMD` carries control word, mode of operation, all three targets (position / velocity / torque-current), profile velocity / accel / decel, command counter. `CYCLIC_STATUS` carries status word, error code, actuals, plus a runtime-configurable telemetry blob (see below).
  - **OD access** (SDO-equivalent): `OD_READ_REQ` / `OD_READ_RESP` / `OD_WRITE_REQ` / `OD_WRITE_RESP` pipelined onto the same transactions in place of `CYCLIC_CMD` for one frame; the slave stages the response and the master picks it up on a subsequent transaction (sequence number for correlation).
- **Runtime telemetry mapping** via OD `0x2A00` (see `INTERFACE_SPEC.md §4a`) — host writes the list of OD entries to stream, motor MCU rebuilds its map atomically, telemetry blob inside `CYCLIC_STATUS` then carries those values each cycle. PDO TX-mapping equivalent.
- **Standard CiA-402 state machine** lives on the motor MCU side; CMC drives it via `controlword` writes in `CYCLIC_CMD`.
- **Operating mode is per-cycle** — `mode_of_operation` is a field in `CYCLIC_CMD`. The wire contract supports Profile Velocity, Profile Position, Cyclic Sync Velocity / Position / Torque, etc. **This CMC's policy default is Profile Velocity**, with CMC-side trajectory generation for shot recall — but that's a CMC choice, not a contract restriction.
- **Frame integrity**: CRC-16/Modbus on header and payload separately. Bad-version / bad-CRC / bad-length frames are rejected via the `ERROR` message type.
- **Timeout / safe-state**: if the slave receives no valid `CYCLIC_CMD` within the command timeout, it raises an SPI-timeout fault and the motor enters the configured safe state.

### 11.3 Network OD exposure (PC ↔ CMC)

The wire format is defined by `Interface/NETWORK_UDP_SPEC.md`. **The summary below is illustrative — Interface is authoritative.**

- **All UDP. No TCP.** Two ports: **5000** for OD access (request/response), **5001** for telemetry stream.
- **Common 8-byte UDP header** (`MC_UdpHeader_t`): magic `0x4D55` (`'MU'`), version byte (== `MC_IF_PROTOCOL_VERSION`), message type, sequence, length. UDP checksum is relied on; no additional CRC.
- **OD access channel** (port 5000): typed messages `OD_READ_REQ` / `OD_READ_RESP` / `OD_WRITE_REQ` / `OD_WRITE_RESP` / `ERROR`. PC assigns the sequence number, CMC echoes it. PC retransmits on timeout (suggested 50 ms × 3). The CMC bridges directly to the motor MCU via the SPI `OD_*_REQ` messages and returns the result.
- **Telemetry channel** (port 5001): PC sends `TLM_SUBSCRIBE` (its receive port, rate-divider against the 1 kHz cyclic, batch size); CMC pushes `TELEMETRY` datagrams (fire-and-forget, batched samples of `MC_IfCyclicStatusHeader_t` + mapped telemetry blob). PC detects drops via per-sample status counter and remaps via `map_version`.
- **The telemetry map (0x2A00) is configured by the PC via OD writes** on port 5000 — those writes are bridged through the CMC to the motor MCU, then the new map is reflected in subsequent telemetry datagrams. Choosing "what to graph" is end-to-end at runtime.
- **No CMC-local OD entries.** The CMC's own configuration (network settings, motor soft limits, auth) lives on the **web** server only and is not exposed via the OD-over-UDP port. The OD network port is a near-transparent bridge to the motor MCU OD; if it's reachable, you can read/write the motor's OD. (This is the clean separation we picked over reserving a CMC OD range — see decisions log.)
- **No authentication** on the OD UDP ports — expose only on a trusted subnet. Operator-facing privileged operations go through the web (HTTP Basic auth).

### 11.4 HTTP

- **HTTP/1.0** with `Connection: close` after every response. No keep-alive.
- **One request at a time** on a single socket. Browser parallelism is limited by closing connections quickly.
- **HTTP Basic authentication** on all pages and JSON endpoints. Realm: `"cmc"`. Credentials are hashed SHA-256 + salt in `config`.
- **No TLS.** LAN device behind a router. Documented constraint.
- **Maximum request size 1 KB.** Larger requests get `413`.
- **No virtual filesystem**: pages are embedded as C string literals.

---

## 12. Build & test strategy

- **Build system**: STM32CubeIDE project, with manually added source folders (`app/`, `bsp/`) and one extra include path (the project root). See `Documentation/build_setup.md`.
- **No desktop simulator.** Hardware-in-the-loop testing only.
- **No bundled tools.** The Python controller emulator from the prior Reduced CMC project lives outside this repo.
- **CubeMX regeneration is safe.** All `.c`/`.h` changes inside `Core/` are confined to `USER CODE BEGIN/END` markers. `app/`, `bsp/`, `Documentation/` are outside CubeMX's reach.
- **Soft tests during bring-up**:
  - Phase 0a: visual LED + ring buffer inspection under debugger.
  - Phase 0b+: real `nc <ip> 30200`, browser interactions, panel poll/select cycle, OD reads via a small Python tool.
- **No unit-test harness** in this repo. If desktop testing becomes necessary, the codec modules (`camerad`, `cia402`) are pure C with no `bsp/` dependencies and can be lifted into a separate test repo.

---

## 13. Implementation phases

| # | Goal | Modules touched | "Done" means | Status |
|---|---|---|---|---|
| 0a | Skeleton (no network) | `main_loop`, `bsp/time`, `bsp/wdg`, `log` (RAM), `config` (RAM) | Boots, LED toggles at 1 Hz, ring buffer fills, IWDG resets when starved | **DONE** |
| 0b | W6100 + TCP log socket | `bsp/net`, `Drivers/w6100`, `log` (TCP) | `nc <ip> 30200` shows heartbeats and boot log | **DONE** (code; awaits hardware bring-up) |
| 1  | UDP/TCP plumbing | `controller_mgr` (poll-listen + accept; dummy handlers) | A panel can poll and get *any* well-formed response | Not started |
| 2  | CAMERAD codec + state | `camerad`, `cmc_state`, `controller_mgr` (full dispatch) | Real S- and T-panels can poll, select, deselect, send key presses; correct shapes returned | Not started |
| 3  | Web + persistent config | `web`, `bsp/flash`, `config` (flash-backed) | Browse to device IP, log in, change settings, persists across reboot | Not started |
| 4  | OD + cia402 + motor | `od`, `cia402`, `motor_ctrl`, `bsp/motor_spi` | Motor MCU is enabled via the `Interface/mc_if_protocol.h` cyclic exchange. Jog from a panel works end-to-end (panel → CMC → CYCLIC_CMD → motor). Soft limits respected. OD bridge handles arbitrary reads/writes pipelined onto the SPI cyclic. | Not started |
| 5  | OD-over-UDP + telemetry stream | `od/od_net` | PC tool reads/writes OD entries on UDP 5000 (`MC_UdpHeader_t` + typed payload, bridged to motor MCU). TLM_SUBSCRIBE on UDP 5001 returns a live telemetry stream batched per `NETWORK_UDP_SPEC.md`. Map changes via OD writes to `0x2A00` are reflected end-to-end (PC observes new `map_version`). | Not started |
| 6  | Hardening | All | Fault paths exercised (SPI fault → motor safe state; UDP packet loss; W6100 reset recovery). Soak test passes. Open points in `Interface/INTERFACE_SPEC.md` closed (scale factors, cyclic rate, SPI clock). | Not started |

Status updates here are part of the maintenance rule: when a phase changes state, the row is updated in the same PR.

---

## 14. Decisions log

| Topic | Decision |
|---|---|
| Ethernet chip | WIZnet W6100 on SPI2 |
| Motor MCU bus | SPI3 |
| I2C2 | Unused (placeholder) |
| Diagnostic output | TCP log socket on port 30200 (no UART, no SWO) |
| Shot storage | ~100 shots in flash, separate dual-bank region |
| Web auth | HTTP Basic, configurable user/pass, default-password flag |
| OD network protocol | `MC_UdpHeader_t` framing per `Interface/NETWORK_UDP_SPEC.md`: UDP 5000 (OD access) + UDP 5001 (telemetry stream). Both ports settable from the web. |
| CMC-local OD entries | **None.** CMC config is web-only. The OD network port is a near-transparent bridge to the motor MCU OD. |
| SPI framing CMC↔Motor | 64-byte fixed full-duplex frame per `Interface/mc_if_protocol.h`. CRC-16/Modbus on header and payload. Cyclic + pipelined-OD on the same transactions. |
| Motor control mode | Wire contract supports PV/PP/CSV/CSP/PT; CMC **default policy** is Profile Velocity with CMC-side trajectory generation for shot recall. Switchable per cycle. |
| Max simultaneous controllers | 2 (W6100 socket budget after the two OD UDP ports + log + HTTP) |
| Build system | STM32CubeIDE project |
| Flash layout | Separate dual-bank regions for `config` and `shots` |
| CANopen node ID | Configurable from web (default 1) |
| Trajectory cadence | ~25 ms velocity setpoint stream during a fade |
| OS / scheduling | Cooperative super-loop, no RTOS |
| Watchdog timeout | 250 ms (IWDG) |
| Per-file size cap | ~400 lines (soft) |

---

## 15. OD index map

The Object Dictionary is owned by the **motor MCU** and defined in `Interface/mc_if_od.h` (`MC_IF_OD_OBJECTS(X)` X-macro). The CMC's OD module is a **bridge** — it does not contribute any local entries.

Conventions (mirrored from `Interface/INTERFACE_SPEC.md §4`):

| Range | Use | Encoding |
|---|---|---|
| `0x1xxx` | CANopen communication profile (device type, error register, identity) | Standard CiA-301 |
| `0x2000–0x29FF` | Manufacturer entries — axis/motor parameters, gains, calibration, faults, persistence, test injection | **float32 in SI units** |
| `0x2A00` | **Telemetry mapping table** (host-configurable list of OD entries to stream) | per `INTERFACE_SPEC.md §4a` |
| `0x6xxx` | CiA-402 standard objects — control word, status word, modes-of-operation, targets, actuals, profile params | Scaled integers (see `mc_if_od.h` scale macros) |

CMC-local configuration (this CMC's network settings, motor soft limits, auth credentials, etc.) is **not** in the OD. It lives in `config` (flash) and is reached via the web UI. This keeps `Interface/` purely a motor-MCU contract — no CMC-specific extensions, no risk of clashing with the motor's `0x2xxx` range.

Detailed motor OD entries: `Interface/mc_if_od.h` (authoritative).

---

## 16. Glossary

| Term | Meaning |
|---|---|
| CMC | Camera Motion Controller — the device this codebase runs on |
| S-panel / T-panel | Shotoku S-type / T-type controller. S has no screen; T has one |
| CAMERAD | Shotoku's controller protocol over UDP+TCP |
| OD | Object Dictionary — the (index, subindex) → value map shared between this CMC and the motor MCU |
| SDO | Service Data Object — the CANopen request/response transaction used to read/write OD entries |
| CiA 402 | CAN-in-Automation drive profile defining control word, status word, modes-of-operation, and standard objects for motor drives |
| PV | Profile Velocity (CiA 402 mode 3). Host writes target velocity; drive runs at it |
| BSP | Board Support Package — the thin layer between application and HAL |
| IWDG | Independent Watchdog (LSI-clocked, survives PLL failure) |
| Nucleo | The STM32 development board variant we target |

---

## 17. Documentation upkeep

(See also the rule at the top.)

A change is "documented" when:

1. The system-level effect (interfaces, behaviour, hardware allocation, persistent state, port numbers) is reflected in this file.
2. The per-module behaviour (API, ownership, acceptance criteria, dependencies) is reflected in the module's `README.md`.
3. Decision drivers (why a choice was made, not just what) are recorded — either in the *Decisions log* table here or as a one-line note in the affected module's README.
4. New documents (`camerad_subset.md`, `cia402_profile.md`, `od_index_map.md`) move from TBD to drafted before the phase that needs them ships.

A change is **not** documented when:

- Code lands without an update here or in the module README.
- The decisions log doesn't reflect a real change (e.g. a port number moves silently).
- A module's README says "TBD" for a behaviour that already exists in code.

If reviewing a change and you cannot tell from this file what the post-change system looks like, the change is incomplete.
