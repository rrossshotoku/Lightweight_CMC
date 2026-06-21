/*
 * app/config — persistent settings.
 *
 * Phase 0a: RAM-only. Defaults applied at init; setters mutate the RAM
 * struct. Phase 0b/4 will back this with bsp/flash for persistence.
 *
 * See app/config/README.md for the contract.
 */

#ifndef APP_CONFIG_H
#define APP_CONFIG_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CONFIG_AUTH_USER_MAX   16
#define CONFIG_AUTH_HASH_LEN   32          /* SHA-256 */
#define CONFIG_AUTH_SALT_LEN   16

typedef struct {
    uint8_t  mac[6];
    uint8_t  ip[4];
    uint8_t  netmask[4];
    uint8_t  gateway[4];
    uint16_t udp_poll_port;       /* CAMERAD poll, default 30002 */
    uint16_t tcp_camerad_port;    /* CAMERAD TCP listen, default 30003 */
    uint16_t http_port;           /* default 80 */
    uint16_t od_udp_port;         /* SDO-over-UDP, default 30100 */
    uint16_t log_tcp_port;        /* log socket, default 30200 */
    uint32_t cmc_device_no;       /* this CMC's CAMERAD device number */
} network_cfg_t;

#define MOTOR_AXIS_COUNT 1   /* Phase 0a: one pan axis. Extend later. */

typedef struct {
    int32_t low_count;
    int32_t high_count;
} axis_limit_t;

typedef struct {
    axis_limit_t axis[MOTOR_AXIS_COUNT];
} motor_limits_t;

typedef struct {
    char     username[CONFIG_AUTH_USER_MAX];
    uint8_t  pass_hash[CONFIG_AUTH_HASH_LEN];
    uint8_t  salt[CONFIG_AUTH_SALT_LEN];
    bool     default_password;   /* true until the user has changed it */
} auth_cfg_t;

void                   config_init(void);                          /* applies defaults */

const network_cfg_t  * config_get_network(void);
bool                   config_set_network(const network_cfg_t *cfg);

const motor_limits_t * config_get_limits(void);
bool                   config_set_limits(const motor_limits_t *lim);

const auth_cfg_t     * config_get_auth(void);
bool                   config_set_auth_password(const char *new_password);

uint8_t                config_get_node_id(void);
bool                   config_set_node_id(uint8_t node_id);

#ifdef __cplusplus
}
#endif

#endif /* APP_CONFIG_H */
