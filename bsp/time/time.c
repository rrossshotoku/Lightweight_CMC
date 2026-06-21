/*
 * bsp/time — monotonic millisecond tick backed by SysTick.
 *
 * SysTick is configured by CubeMX in HAL_Init to fire at 1 kHz, calling
 * HAL_IncTick each interrupt. We override HAL_GetTick (declared weak in
 * stm32g4xx_hal.c) so the entire HAL — and our app — share one time source.
 */

#include "time.h"
#include "stm32g4xx_hal.h"

void time_init(void)
{
    /* SysTick was started by HAL_Init() before main_loop_init runs.
     * Nothing to do here today; the function exists so the init
     * order in main_loop is uniform across modules. */
}

uint32_t time_ms(void)
{
    return HAL_GetTick();
}

uint32_t time_elapsed_ms(uint32_t since)
{
    /* uint32 subtraction is wrap-safe across the 49.7-day rollover. */
    return time_ms() - since;
}

bool time_after(uint32_t a, uint32_t b)
{
    /* True if a is later than b under wrap-safe arithmetic.
     * Treats a "distance" of more than 2^31 as wrap-around. */
    return (int32_t)(a - b) > 0;
}
