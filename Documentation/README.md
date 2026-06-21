# Documentation

Authoritative documentation **for the Lightweight CMC firmware specifically**.

For the **cross-project boundary contracts** (wire formats and OD layout shared with the motor-control MCU and the PC tool), see [`../Interface/`](../Interface/README.md). Those files are the single source of truth for the SPI link and the UDP-over-Ethernet protocols. They are not maintained here — this folder defers to them, and any change to Interface bumps `MC_IF_PROTOCOL_VERSION`.

## ⚠ Maintenance rule

**This documentation is the contract for how the system is built. Any change to the architecture, interfaces, hardware allocation, or protocol behaviour MUST update the corresponding document in the same change that touches the code.**

If you find a disagreement between code and documentation, the documentation is authoritative — open an issue, do not silently change the code (or, separately, the docs) to "fix" the mismatch. Either the code is wrong and needs fixing, or the design changed and the docs need updating; both decisions require the same conversation.

Reviewers: do not approve a change that touches a module without checking that the module's `README.md` and any affected page here are consistent with it.

## Contents

| File | What it covers |
|---|---|
| [`architecture.md`](architecture.md) | Master design document for the CMC firmware. Layering rules, module catalogue, data flows, persistent state, network design, decisions log. Defers to `Interface/` for wire formats and OD layout |
| [`build_setup.md`](build_setup.md) | One-time CubeIDE project setup and per-phase runtime checks |
| `camerad_subset.md` (TBD)            | Exact subset of CAMERAD this CMC implements as a server (opcodes, body shapes, response shapes per device type). Will be drafted alongside Phase 2. CAMERAD is *not* in Interface — it's a Shotoku panel protocol independent of the motor-MCU link |

The previously-mentioned `cia402_profile.md` and `od_index_map.md` are no longer planned — those concerns are owned by `Interface/INTERFACE_SPEC.md` and `Interface/mc_if_od.h` respectively.

Each module also has a `README.md` in its own folder (under `app/` and `bsp/`) describing its **purpose, ownership, public API, dependencies, acceptance criteria, and notes**. Those READMEs are the per-module contracts; this folder is the system-level contract.

## How the documents relate

```
            ┌─────────────────────────────┐
            │  Interface/                 │  <- cross-project boundary
            │   INTERFACE_SPEC.md         │     (SPI + UDP wire formats,
            │   NETWORK_UDP_SPEC.md       │      OD layout, shared by motor
            │   mc_if_protocol.h          │      MCU firmware and PC tool)
            │   mc_if_od.h                │
            └──────────────┬──────────────┘
                           │ referenced by
                           ▼
            ┌─────────────────────────────┐
            │  Documentation/             │  <- CMC system-level contract
            │   architecture.md           │
            │   build_setup.md            │
            │   (camerad_subset.md TBD)   │
            └──────────────┬──────────────┘
                           │ defines the boundaries
                           ▼
            ┌─────────────────────────────┐
            │  app/<module>/README.md     │  <- per-module contract
            │  bsp/<module>/README.md     │
            └──────────────┬──────────────┘
                           │ describes the API and behaviour of
                           ▼
            ┌─────────────────────────────┐
            │  app/<module>/<module>.c    │  <- the implementation
            │  bsp/<module>/<module>.c    │
            └─────────────────────────────┘
```

A change at any level requires walking up to make sure the levels above it still describe reality. A change at the **Interface/** level additionally requires bumping `MC_IF_PROTOCOL_VERSION` and flagging the other consumers (motor MCU firmware, PC tool) so they can rebuild.
