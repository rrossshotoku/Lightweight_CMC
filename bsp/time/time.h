/*
 * bsp/time — monotonic millisecond tick.
 *
 * Sole time source for the codebase. No other module calls HAL_GetTick directly.
 * See bsp/time/README.md for the contract.
 */

#ifndef BSP_TIME_H
#define BSP_TIME_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

void     time_init(void);
uint32_t time_ms(void);
uint32_t time_elapsed_ms(uint32_t since);
bool     time_after(uint32_t a, uint32_t b);

#ifdef __cplusplus
}
#endif

#endif /* BSP_TIME_H */
