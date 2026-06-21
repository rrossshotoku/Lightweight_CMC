# app/main_loop

## Purpose
Orchestrator. Owns module initialisation order and runs the cooperative event tick. Sole caller of `wdg_kick()`.

## Owns
- The startup sequence (`main_loop_init`) calling every module's `*_init` in dependency order.
- The event loop (`main_loop_run`) calling every module's `*_tick` once per pass.
- The watchdog kick cadence — exactly once per tick.

## Does NOT do
- Any protocol parsing, motor commanding, network I/O. Only orchestration.
- Any blocking calls. If a tick handler ever blocks, the watchdog will reset.

## Public API
```c
void main_loop_init(void);   // call once from main() before main_loop_run
void main_loop_run(void);    // never returns
```

## Dependencies
- Every other module's `*_init` and `*_tick` symbols.
- `bsp/wdg`, `bsp/time`.

## Acceptance criteria
- Boots from cold to "all ticks running" in < 500 ms.
- Tick period < 5 ms under normal load.
- IWDG kicks observed on logic-analyser at the correct cadence.
- Removing any one module's `*_tick` call from the loop disables only that module.

## Notes
- Init order is documented in this README and enforced by the compiler (linker error if a module references another that hasn't been declared).
- The tick order is documented inline in `main_loop.c`. It matters: `controller_mgr_tick` after `bsp/net` poll, before `cmc_state` housekeeping, etc.
