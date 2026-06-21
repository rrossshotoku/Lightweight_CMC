/*
 * bsp/net/wizchip_glue.h — internal port layer for the WIZnet ioLibrary.
 *
 * Not a public API. Only bsp/net/net.c should include this. It exists as a
 * separate file to keep the hardware-specific SPI/CS/RESET code out of the
 * socket-API file, and to make the CubeMX pin overrides obvious.
 */

#ifndef BSP_NET_WIZCHIP_GLUE_H
#define BSP_NET_WIZCHIP_GLUE_H

#include <stdbool.h>
#include <stdint.h>

/*----------------------------------------------------------------------------
 * Pin assignment.
 *
 * Configure these in the CubeMX .ioc by giving the pins the labels
 * W6100_CS, W6100_RST and (optionally) W6100_INT. CubeMX then emits
 * matching macros in Core/Inc/main.h and the #ifndef guards below pick
 * them up. If you have not yet labelled the pins, edit the defaults
 * here as a temporary measure (and tell the user to fix the .ioc).
 *
 * Defaults are inherited from the reference uc_camd_interface project:
 *   CS  = PB12
 *   RST = PC8
 *---------------------------------------------------------------------------*/

#include "main.h"            /* CubeMX-generated pin label macros */

#ifndef W6100_CS_GPIO_Port
#define W6100_CS_GPIO_Port   GPIOB
#define W6100_CS_Pin         GPIO_PIN_12
#endif

#ifndef W6100_RST_GPIO_Port
#define W6100_RST_GPIO_Port  GPIOC
#define W6100_RST_Pin        GPIO_PIN_8
#endif

/*----------------------------------------------------------------------------
 * Lifecycle
 *---------------------------------------------------------------------------*/

/* Drive the W6100 RST line through a reset pulse: CS high, RST low 10 ms,
 * RST high, wait 150 ms for the chip to come up. Blocking; called only at
 * net_init time. */
void wizchip_glue_reset_pulse(void);

/* Register the CRIS, CS and SPI callbacks with the ioLibrary. Must be
 * called after wizchip_glue_reset_pulse(). */
void wizchip_glue_register_callbacks(void);

#endif /* BSP_NET_WIZCHIP_GLUE_H */
