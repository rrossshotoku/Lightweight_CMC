/*
 * app/main_loop — Phase 0b orchestrator.
 *
 * Init order (top of file = first to run):
 *   1. bsp/time     — timekeeping
 *   2. bsp/wdg      — watchdog, latched on (250 ms)
 *   3. app/log      — RAM ring buffer ready to capture init messages
 *   4. app/config   — defaults populated
 *   5. log port set — log_tick can now know where to listen
 *   6. bsp/net      — W6100 brought up; static IP applied
 *
 * Tick order, repeated forever:
 *   1. wdg_kick()                            — once per pass, only here
 *   2. log_tick()                            — service TCP log socket
 *   3. heartbeat (LED toggle, log every 1 s)
 *
 * Phase 1+ will add controller_mgr, web, motor_ctrl, od, cia402 ticks here.
 */

#include "main_loop.h"

#include "app/log/log.h"
#include "app/config/config.h"
#include "bsp/time/time.h"
#include "bsp/wdg/wdg.h"
#include "bsp/net/net.h"

#include "stm32g4xx_hal.h"
#include "stm32g4xx_nucleo.h"   /* for BSP_LED_Toggle */

/* Heartbeat: blink LED + INFO log once per second. */
#define HEARTBEAT_PERIOD_MS  1000

void main_loop_init(void)
{
    time_init();
    wdg_init(250);                          /* 250 ms timeout */
    log_init();
    config_init();

    LOG_INFO("Lightweight CMC boot");

    const network_cfg_t *net = config_get_network();
    LOG_INFO("node_id=%u ip=%u.%u.%u.%u",
             config_get_node_id(),
             net->ip[0], net->ip[1], net->ip[2], net->ip[3]);

    log_set_tcp_port(net->log_tcp_port);

    /* Bring up the W6100. On failure, the device still ticks (heartbeat
     * + watchdog) so we can spot the failure in the log ring under the
     * debugger. The log TCP socket simply never opens, and log_tick
     * keeps retrying its prerequisites. */
    if (!net_init(net->mac, net->ip, net->netmask, net->gateway)) {
        LOG_ERROR("net_init failed — continuing without network");
    }
}

void main_loop_run(void)
{
    uint32_t last_beat = time_ms();
    uint32_t beats = 0;

    for (;;) {
        wdg_kick();
        log_tick();

        if (time_elapsed_ms(last_beat) >= HEARTBEAT_PERIOD_MS) {
            last_beat += HEARTBEAT_PERIOD_MS;
            beats++;
            BSP_LED_Toggle(LED_GREEN);
            LOG_INFO("heartbeat %lu dropped_log_lines=%lu",
                     (unsigned long)beats,
                     (unsigned long)log_dropped_lines());
        }

        /* No sleep — the orchestrator polls. Phase 0b adds the network
         * service which produces real work; until then this is a tight
         * loop deliberately, so we can confirm IWDG behaviour. */
    }
}
