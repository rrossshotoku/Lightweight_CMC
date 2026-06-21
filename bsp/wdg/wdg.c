/*
 * bsp/wdg — IWDG on the STM32G431.
 *
 * IWDG clock = LSI ~32 kHz (the actual rate is part-to-part variable but
 * the watchdog tolerates ~25-40 kHz). With the largest prescaler (256) the
 * counter ticks at ~125 Hz, giving an 8 ms resolution and a maximum window
 * of ~32 s. We target a default 250 ms timeout: prescaler 32 (LSI/32 ~1 kHz),
 * reload = timeout_ms.
 *
 * Note: IWDG cannot be disabled once started; debug-mode freeze is configured
 * via DBGMCU (HAL_DBGMCU_FreezeIWDG) so breakpoints don't trip it.
 */

#include "wdg.h"
#include "stm32g4xx_hal.h"

static IWDG_HandleTypeDef s_iwdg;

void wdg_init(uint32_t timeout_ms)
{
    /* Freeze IWDG when the debugger halts the core. Keeps breakpoints sane. */
    __HAL_DBGMCU_FREEZE_IWDG();

    /* LSI prescaler 32 → counter at ~1 kHz → reload counts ≈ ms.
     * Clamp to the 12-bit reload register range. */
    if (timeout_ms < 1)    timeout_ms = 1;
    if (timeout_ms > 4095) timeout_ms = 4095;

    s_iwdg.Instance       = IWDG;
    s_iwdg.Init.Prescaler = IWDG_PRESCALER_32;
    s_iwdg.Init.Window    = IWDG_WINDOW_DISABLE;
    s_iwdg.Init.Reload    = timeout_ms;

    if (HAL_IWDG_Init(&s_iwdg) != HAL_OK) {
        /* Cannot recover — IWDG init failure leaves the device unprotected.
         * Spin here so the bench will notice. Production builds should fault
         * via Error_Handler instead, but Error_Handler itself will use this. */
        while (1) { }
    }
}

void wdg_kick(void)
{
    /* Refresh — does nothing if IWDG isn't running. */
    HAL_IWDG_Refresh(&s_iwdg);
}

void wdg_force_reset(void)
{
    /* Deliberate reset path: stop kicking and wait. */
    __disable_irq();
    for (;;) { __NOP(); }
}
