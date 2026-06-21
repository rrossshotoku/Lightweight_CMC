/*
 * bsp/wdg — Independent Watchdog.
 *
 * Initialised once at boot, kicked exactly once per main_loop tick.
 * See bsp/wdg/README.md for the contract.
 */

#ifndef BSP_WDG_H
#define BSP_WDG_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void wdg_init(uint32_t timeout_ms);
void wdg_kick(void);
void wdg_force_reset(void);

#ifdef __cplusplus
}
#endif

#endif /* BSP_WDG_H */
