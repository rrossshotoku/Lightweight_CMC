# Build setup (CubeIDE)

The `app/`, `bsp/`, and `Drivers/w6100/` trees are outside CubeMX's auto-managed area, so CubeIDE needs to be told about them once. Re-running CubeMX from the `.ioc` does not touch them.

## One-time project setup

1. **Add source folders.** Right-click the project in CubeIDE → *Properties* → *C/C++ General* → *Paths and Symbols* → *Source Location* → *Add Folder…* — add each of:
   - `app`
   - `bsp`
   - `Drivers/w6100`
2. **Add include paths.** In the same dialog → *Includes* tab → *GNU C* configuration → *Add…* — add each of:
   - the project root (workspace selection → project; equivalent to `${ProjDirPath}` / `.`),
   - `Drivers/w6100`,
   - `Drivers/w6100/W6100`.
3. **Confirm Nucleo BSP is in the build.** CubeMX's `.ioc` has `NUCLEO-G431RB` selected, so `Drivers/BSP/STM32G4xx_Nucleo` should already be compiled. Spot-check by searching for `stm32g4xx_nucleo.h` in the Project Explorer.
4. **Build.** Should compile cleanly. If you see `fatal error: app/main_loop/main_loop.h: No such file`, you missed step 2.

## W6100 wiring (required for Phase 0b)

The `.ioc` must configure two GPIO outputs (CS and RST) and one SPI bus (W6100 data) before Phase 0b code will work on real hardware:

| Function | Pin in reference project | Notes |
|---|---|---|
| SPI bus  | SPI2 (already configured)   | Mode: master, full-duplex, 8-bit, MSB first, baud ≤ 21 MHz. SCK/MISO/MOSI per the Nucleo header you're using |
| CS  GPIO | `PB12` (label `W6100_CS`)   | Output push-pull, no pull, **default level HIGH** |
| RST GPIO | `PC8`  (label `W6100_RST`)  | Output push-pull, no pull, **default level HIGH** |
| (optional) INT GPIO | unused this revision | Polled mode; no EXTI line wired |

Open the `.ioc`, click each pin in *Pinout view*, set its mode and user label, set initial level to HIGH, then *Project → Generate Code*. CubeMX will emit the `..._GPIO_Port` and `..._Pin` macros in `Core/Inc/main.h`, which `bsp/net/wizchip_glue.h` picks up via `#ifndef` guards.

If you can't label the pins yet, the defaults in `bsp/net/wizchip_glue.h` are `PB12 / PC8` — change them there temporarily but fix the `.ioc` before checking in.

## What Phase 0a does when it runs

After flashing and resetting:

- Green LED on the Nucleo toggles once per second.
- The RAM ring buffer at `s_ring` (inside `app/log/log.c`) accumulates lines like:

  ```
  [000000] INFO  Lightweight CMC boot
  [000000] INFO  node_id=1 ip=192.168.1.50
  [001000] INFO  heartbeat 1 dropped_log_lines=0
  [002000] INFO  heartbeat 2 dropped_log_lines=0
  ...
  ```

  You can read it under the debugger by inspecting `s_ring`, `s_head`, `s_tail` in `app/log/log.c`, or by calling `log_peek` from a debugger-injected expression.

- The IWDG runs at 250 ms timeout. To confirm it works, set a breakpoint somewhere in `main_loop_run` and let it sit longer than 250 ms — the device should reset.
- The push-button is initialised in EXTI mode by CubeMX but unused.

## What Phase 0b adds

- `bsp/net` brings the W6100 up (reset pulse → callback registration → chip-ID check → PHY-link wait → network info → socket buffer init).
- `app/log` opens a TCP listen socket on the configured `log_tcp_port` (default 30200) once `net_link_up()` is true.
- `nc <device_ip> 30200` from any machine on the LAN should show the boot log followed by live heartbeats:

  ```
  $ nc 192.168.1.50 30200
  [001000] INFO  Lightweight CMC boot
  [001000] INFO  node_id=1 ip=192.168.1.50
  [001100] INFO  net: W6100 chip ID 0x6100
  [001120] INFO  net: PHY link up
  [001120] INFO  net: ip=192.168.1.50 mask=255.255.255.0 gw=192.168.1.1
  [001500] INFO  [log: client connected]
  [002000] INFO  heartbeat 1 dropped_log_lines=0
  ...
  ```

- Closing the `nc` session leaves the device in LISTEN again; another `nc` reconnects without reset.

## What Phase 0b does *not* do

- Talk CAMERAD (Phase 2).
- Touch the motor SPI (Phase 4).
- Persist anything to flash (Phase 4).
- Open the OD network port, the HTTP server, or the CAMERAD UDP/TCP listeners — those are pre-allocated socket slots in `bsp/net`'s static map but no app module owns them yet.

## Common build errors

**`fatal error: wizchip_conf.h: No such file or directory`** (during build of `Drivers/w6100/W6100/w6100.c` or `w6100.h`):

The W6100 vendor source uses unqualified `#include "wizchip_conf.h"` — that header lives at `Drivers/w6100/wizchip_conf.h`. The compiler can only find it if `Drivers/w6100` is on the include path.

Fix: re-open *Properties → C/C++ General → Paths and Symbols → Includes (GNU C)* and confirm that **all three** of these are present (Workspace-relative is fine):

- `${ProjDirPath}` — the project root
- `${ProjDirPath}/Drivers/w6100`
- `${ProjDirPath}/Drivers/w6100/W6100`

Tick **Add to all configurations** when adding each, so Debug and Release both see them. CubeIDE may prompt to rebuild the index — accept. Then *Project → Clean → Build*.

Same symptom with a different missing header (`socket.h`, `w6100.h`) usually means the same root cause: one of the include paths above is missing or not applied to your active build configuration.

## Common Phase 0b pitfalls

- **Device unreachable on the LAN**: check the default IP (192.168.1.50/24) is on your subnet, or change it via `app/config/config.c` defaults until Phase 3 wires the web config.
- **`net_init` fails with bad chip ID**: SPI wiring or speed. Check MISO/MOSI/SCK and try a slower baud rate in the `.ioc`. The reference project succeeded at 21 MHz APB / prescaler 4 ≈ 5 MHz.
- **PHY link timeout**: cable unplugged, no link partner. Plug into a switch or PC.
- **`nc` connects but sees nothing**: the device booted before you connected and the ring filled. Check `dropped_lines` in the next heartbeat — anything > 0 means the ring overflowed while no one was listening.
- **CubeIDE freezes the IWDG on breakpoint by default** because we call `__HAL_DBGMCU_FREEZE_IWDG()` in `wdg_init`. If you want to test the IWDG reset behaviour, comment that line temporarily — but remember to put it back, or every breakpoint resets the device.
- **HAL_GetTick is overridden implicitly** because our SysTick handler still calls `HAL_IncTick`; `time_ms` then reads `HAL_GetTick`. If you ever switch to a hardware timer for the OS tick instead, you must replace `HAL_GetTick` with a `__weak` override pointing at the new source.
- **No floats in log_printf.** `LOG_INFO("value=%.2f", x)` will not work — nano-newlib does not link the float-printf path. Use integer scaling: `LOG_INFO("value=%ld.%02ld", x_int, x_frac)`.
