# Lightweight CMC

A reduced Camera Motion Controller (CMC) for the Shotoku ecosystem.

## What it does

- Speaks **CAMERAD** to S-type and T-type controller panels over Ethernet (UDP discovery + TCP control channel).
- Talks **CiA 402-style** (Profile Velocity) over SPI to a separate motor controller MCU, with a shared **object dictionary**.
- Exposes the object dictionary on the network using **CANopen SDO-over-UDP** (8-byte SDO frame as the UDP payload).
- Hosts a small **web UI** for network configuration and motor limit setting, behind HTTP Basic authentication.
- Sends diagnostic logs over a TCP socket (no UART/SWO).

## What it does *not* do

- Run a full CANopen master/slave stack on a CAN bus.
- Implement CiA 402 Profile Position or Cyclic Synchronous Position (Profile Velocity only — CMC computes trajectories for shot recalls itself).
- Speak any CAMERAD message family beyond what S/T panels send (no CCU, NHM, on-air broadcasts, zone info, cue computer).
- Persist any data the user cannot reproduce — settings, motor limits, and ~100 shots only.

## Target hardware

- **MCU**: STM32G431RBTx on Nucleo-G431RB (170 MHz, Cortex-M4)
- **Network**: WIZnet W6100 on SPI2
- **Motor MCU**: external, on SPI3
- **I2C2**: present but unused (placeholder)
- **No UART/SWO** for trace — logs go over the network

## Repo layout

```
Core/                       CubeMX-generated, untouched
Drivers/                    CubeMX HAL + vendor drivers
  ├── STM32G4xx_HAL_Driver/   (CubeMX)
  ├── BSP/  CMSIS/            (CubeMX)
  └── w6100/                  (WIZnet ioLibrary, vendor)
app/                        application code
  ├── main_loop/              orchestrator
  ├── camerad/                CAMERAD protocol codec (stateless)
  ├── controller_mgr/         controller lifecycle + dispatch
  ├── cmc_state/              single source of truth (selection, status, shots)
  ├── motor_ctrl/             high-level motor API + trajectory generator
  ├── cia402/                 CiA 402 state machine + SDO codec
  ├── od/                     object dictionary registry + UDP network port
  ├── web/                    HTTP server + pages + JSON API
  ├── config/                 persistent settings + limits
  └── log/                    TCP log socket + RAM ring buffer
bsp/                        thin wrappers over the HAL
  ├── net/                    socket API over W6100
  ├── motor_spi/              framed SPI transfer to motor MCU
  ├── flash/                  internal flash, dual-bank versioned storage
  ├── time/                   monotonic ms tick
  └── wdg/                    IWDG init + kick
docs/                       architecture, protocol, profile docs
```

## Build

STM32CubeIDE project. Open `.ioc` in CubeMX to regen Core/Drivers, then build inside CubeIDE.

## Documentation

There are **two** documentation sources, with different scopes and authority:

### `Interface/` — cross-project boundary (authoritative)

Defines the wire formats that this CMC, the motor-control MCU firmware, and the PC tool **all** consume:

- [`Interface/INTERFACE_SPEC.md`](Interface/INTERFACE_SPEC.md) — SPI link CMC ↔ motor MCU (64-byte full-duplex frame, cyclic + pipelined OD, telemetry mapping).
- [`Interface/NETWORK_UDP_SPEC.md`](Interface/NETWORK_UDP_SPEC.md) — UDP protocol PC ↔ CMC (OD access on port 5000, telemetry stream on port 5001).
- [`Interface/mc_if_protocol.h`](Interface/mc_if_protocol.h), [`Interface/mc_if_od.h`](Interface/mc_if_od.h) — frozen C headers consumed by all three projects.

**Changes to `Interface/` are deliberate** — they bump `MC_IF_PROTOCOL_VERSION` and must be flagged before editing so the motor MCU firmware and PC tool can be rebuilt against the same version.

### `Documentation/` — Lightweight CMC project docs

Everything about how *this* CMC firmware is built:

- [`Documentation/architecture.md`](Documentation/architecture.md) — master design contract for the CMC. Defers to `Interface/` for wire formats and OD layout.
- [`Documentation/build_setup.md`](Documentation/build_setup.md) — one-time CubeIDE project setup.

Each module additionally has its own `README.md` under `app/<module>/` or `bsp/<module>/` describing its contract.

> ⚠ **Documentation must be kept up to date.** Any change to the architecture, interfaces, hardware allocation, persistent state, port numbers, or protocol behaviour must update the relevant document in the *same change* as the code. Code reviews must check this. See `Documentation/README.md` for the full upkeep rule.

## Design contract

See `Documentation/architecture.md`. Hard rules in short:

- Dependencies flow **downward only** (app → bsp → Drivers).
- `cmc_state` is the **single source of truth** for runtime state.
- `camerad` and `cia402` are **stateless codecs** — pure byte transforms.
- No `HAL_Delay`. No floats on the wire. No `while(1)` in error paths.
- Watchdog on from day one.
- Per-file soft cap ~400 lines, per-function ~100 lines.

## Status

Planning phase. Module READMEs under `app/*/` and `bsp/*/` describe the contract for each block before any `.c` is written.
