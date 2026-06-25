/*
 * app/config — Phase 0a: RAM-only defaults.
 *
 * No flash backing yet. Settings are reset to defaults on every boot.
 * Phase 4 will wire this through bsp/flash for persistence.
 */

#include "config.h"
#include "app/log/log.h"
#include "app/persist/persist.h"
#include "bsp/identity/identity.h"
#include <string.h>

static network_cfg_t  s_network;
static motor_limits_t s_limits;
static auth_cfg_t     s_auth;
static uint8_t        s_node_id;

/* On-flash network blob. Only the operator-tunable subset is persisted.
 * Bump NETWORK_PERSIST_VERSION on any layout change so stale blobs are
 * rejected by persist_load (caller falls back to coded defaults). */
#define NETWORK_PERSIST_VERSION  1u
#define NETWORK_PERSIST_MAGIC    0x4E455457u    /* "NETW" little-endian */

typedef struct __attribute__((packed)) {
    uint32_t magic;            /*  4 */
    uint8_t  ip[4];            /*  4 */
    uint8_t  netmask[4];       /*  4 */
    uint8_t  gateway[4];       /*  4 */
    uint32_t cmc_device_no;    /*  4 */
    uint32_t reserved[3];      /* 12 — room for one more u32 without a version bump */
} network_persist_blob_t;      /* total: 32 */

_Static_assert(sizeof(network_persist_blob_t) == 32,
               "network_persist_blob_t layout drift");

void config_init(void)
{
    /* Defaults. MAC is derived per-unit by bsp/identity (today: from the
     * STM32 UID; later: from an SPI-connected identity device). IP is a
     * recognisable placeholder until the web UI sets it. */
    memset(&s_network, 0, sizeof(s_network));
    identity_get_mac(s_network.mac);
    s_network.ip[0]      = 192; s_network.ip[1]      =   1; s_network.ip[2]      = 0; s_network.ip[3]      = 100;
    s_network.netmask[0] = 255; s_network.netmask[1] = 255; s_network.netmask[2] = 255; s_network.netmask[3] = 0;
    s_network.gateway[0] = 192; s_network.gateway[1] =   1; s_network.gateway[2] = 0; s_network.gateway[3] = 1;
    s_network.udp_poll_port    = 30002;
    s_network.tcp_camerad_port = 30003;     /* Reduced CMC default (uc_camd_interface config_flash.h:50). The TCP listener port we advertise in poll responses. The actual TCP exchange uses outbound from CMC to panel's return_port (typically 30001). */
    s_network.http_port        = 80;
    s_network.od_udp_port      = 5000;
    s_network.log_tcp_port     = 30200;
    s_network.cmc_device_no    = 1;

    /* Try to overlay the operator-tunable fields from flash. On any
     * failure (uninitialised region, CRC mismatch, version bump) the
     * defaults set above stand. The fields outside the persisted set
     * (ports, MAC) always come from the defaults — they are wire
     * conventions, not operator settings. */
    network_persist_blob_t blob;
    size_t got = 0;
    if (persist_load(PERSIST_REGION_NETWORK, &blob, sizeof(blob),
                     NETWORK_PERSIST_VERSION, &got)
        && got == sizeof(blob)
        && blob.magic == NETWORK_PERSIST_MAGIC) {
        memcpy(s_network.ip,      blob.ip,      4);
        memcpy(s_network.netmask, blob.netmask, 4);
        memcpy(s_network.gateway, blob.gateway, 4);
        s_network.cmc_device_no = blob.cmc_device_no;
        LOG_INFO("config: network loaded from flash (ip=%u.%u.%u.%u dev=%lu)",
                 s_network.ip[0], s_network.ip[1], s_network.ip[2], s_network.ip[3],
                 (unsigned long)s_network.cmc_device_no);
    } else {
        LOG_INFO("config: network using factory defaults");
    }

    memset(&s_limits, 0, sizeof(s_limits));
    /* Default to "no limit" by setting a wide range; motor_ctrl will treat
     * a low==high pair as "disabled". Tighten per-axis from the web. */
    for (size_t i = 0; i < MOTOR_AXIS_COUNT; i++) {
        s_limits.axis[i].low_count  = -2147483647;
        s_limits.axis[i].high_count =  2147483647;
    }

    memset(&s_auth, 0, sizeof(s_auth));
    /* Factory default — flagged so the device can refuse production
     * features until the user has set a real password. */
    strncpy(s_auth.username, "admin", sizeof(s_auth.username) - 1);
    s_auth.default_password = true;

    s_node_id = 1;
}

const network_cfg_t * config_get_network(void) { return &s_network; }

bool config_set_network(const network_cfg_t *cfg)
{
    if (!cfg) return false;
    s_network = *cfg;
    return true;
}

const motor_limits_t * config_get_limits(void) { return &s_limits; }

bool config_set_limits(const motor_limits_t *lim)
{
    if (!lim) return false;
    s_limits = *lim;
    return true;
}

const auth_cfg_t * config_get_auth(void) { return &s_auth; }

bool config_set_auth_password(const char *new_password)
{
    if (!new_password || new_password[0] == '\0') return false;
    /* Phase 0a stub: store the literal string in the hash slot. Phase 4
     * replaces this with a real SHA-256(salt || password). */
    size_t n = strlen(new_password);
    if (n > sizeof(s_auth.pass_hash)) n = sizeof(s_auth.pass_hash);
    memset(s_auth.pass_hash, 0, sizeof(s_auth.pass_hash));
    memcpy(s_auth.pass_hash, new_password, n);
    s_auth.default_password = false;
    return true;
}

uint8_t config_get_node_id(void)            { return s_node_id; }

bool config_set_node_id(uint8_t node_id)
{
    /* CANopen valid range 1..127 */
    if (node_id < 1 || node_id > 127) return false;
    s_node_id = node_id;
    return true;
}

bool config_save_network_to_flash(void)
{
    network_persist_blob_t blob;
    memset(&blob, 0, sizeof(blob));
    blob.magic         = NETWORK_PERSIST_MAGIC;
    memcpy(blob.ip,      s_network.ip,      4);
    memcpy(blob.netmask, s_network.netmask, 4);
    memcpy(blob.gateway, s_network.gateway, 4);
    blob.cmc_device_no = s_network.cmc_device_no;

    bool ok = persist_save(PERSIST_REGION_NETWORK, &blob, sizeof(blob),
                           NETWORK_PERSIST_VERSION);
    if (ok) {
        LOG_INFO("config: network saved to flash (ip=%u.%u.%u.%u dev=%lu)",
                 s_network.ip[0], s_network.ip[1], s_network.ip[2], s_network.ip[3],
                 (unsigned long)s_network.cmc_device_no);
    } else {
        LOG_ERROR("config: network save FAILED");
    }
    return ok;
}
