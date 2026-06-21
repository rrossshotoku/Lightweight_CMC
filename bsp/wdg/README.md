# bsp/wdg

## Purpose
Independent Watchdog (IWDG). Initialised once at boot, kicked exactly once per `main_loop` tick. Resets the device if the main loop hangs.

## Owns
- IWDG init with a fixed timeout (default 250 ms).
- The kick function.

## Does NOT do
- Decide when to kick. Only `main_loop` calls `wdg_kick`. Other modules must not kick the watchdog from inside an error retry loop — that defeats it.

## Public API
```c
void wdg_init(uint32_t timeout_ms);   /* call once before any tick */
void wdg_kick(void);                  /* called from main_loop, no one else */
void wdg_force_reset(void);           /* deliberate reset path — busy-wait on IWDG to expire */
```

## Dependencies
- `Drivers/STM32G4xx_HAL_Driver` (IWDG HAL).

## Acceptance criteria
- After `wdg_init(250)`, ceasing to call `wdg_kick` results in a reset within 250 ms ±10%.
- IWDG remains active across software resets (its config is locked at init by the LSI).
- `wdg_force_reset` returns control to the caller via reset within 250 ms.

## Notes
- IWDG runs from LSI (~32 kHz), which is independent of the main clock. Survives PLL failure.
- IWDG cannot be disabled once enabled (until reset). This is intentional.
- Document the timeout in `docs/architecture.md` cross-cutting section so it isn't silently bumped.
- Halting the debugger normally feeds the IWDG via the DBGMCU register settings — confirm those are active for debug builds, or the device will reset every time you stop at a breakpoint.
