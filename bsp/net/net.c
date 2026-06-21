/*
 * bsp/net — W6100 socket-like API implementation.
 *
 * Wraps the WIZnet ioLibrary (Drivers/w6100) behind the API in net.h.
 * Static socket allocation: callers supply the hardware slot they want
 * (per the architecture's socket map). No runtime pool, no reservations.
 *
 * Reference: working bring-up patterns from
 *   networked_node/microcontroller/uc_camd_interface/Core/Src/wizchip_port.c
 * and Core/Src/app_init.c, deliberately simplified.
 */

#include "net.h"
#include "wizchip_glue.h"

#include "socket.h"             /* Drivers/w6100 */
#include "wizchip_conf.h"       /* Drivers/w6100 */

#include "app/log/log.h"
#include "bsp/time/time.h"

#include <string.h>

/*----------------------------------------------------------------------------
 * Module state
 *---------------------------------------------------------------------------*/

static bool s_initialised = false;

/*----------------------------------------------------------------------------
 * Helpers
 *---------------------------------------------------------------------------*/

static bool valid_sock(net_sock_t sock)
{
    return sock >= 0 && sock < _WIZCHIP_SOCK_NUM_;
}

static net_tcp_state_t map_sn_sr(uint8_t sr)
{
    switch (sr) {
        case SOCK_CLOSED:      return NET_TCP_CLOSED;
        case SOCK_INIT:        return NET_TCP_INIT;
        case SOCK_LISTEN:      return NET_TCP_LISTEN;
        case SOCK_ESTABLISHED: return NET_TCP_ESTABLISHED;
        case SOCK_CLOSE_WAIT:  return NET_TCP_CLOSE_WAIT;
        default:               return NET_TCP_OTHER;
    }
}

static bool wait_phy_link(uint32_t timeout_ms)
{
    uint32_t start = time_ms();
    while (wizphy_getphylink() != PHY_LINK_ON) {
        if (time_elapsed_ms(start) > timeout_ms) return false;
        /* IWDG must keep ticking — main_loop kicks it. We don't block
         * here for longer than the IWDG period; the caller's outer
         * loop will catch a hang. */
        HAL_Delay(10);
    }
    return true;
}

/*----------------------------------------------------------------------------
 * Lifecycle
 *---------------------------------------------------------------------------*/

bool net_init(const uint8_t mac[6],
              const uint8_t ip[4],
              const uint8_t netmask[4],
              const uint8_t gateway[4])
{
    if (s_initialised) return true;

    wizchip_glue_reset_pulse();
    wizchip_glue_register_callbacks();

    /* Confirm we can talk to the chip at all. The W6100 reports
     * 0x6100 in CIDR. */
    uint16_t chip_id = getCIDR();
    if (chip_id != 0x6100) {
        LOG_ERROR("net: bad W6100 chip ID 0x%04X (expected 0x6100)", chip_id);
        return false;
    }
    LOG_INFO("net: W6100 chip ID 0x%04X", chip_id);

    /* Unlock the network registers before changing them. */
    uint8_t syslock = SYS_NET_LOCK;
    ctlwizchip(CW_SYS_UNLOCK, &syslock);

    /* Wait for the PHY to come up. 3 s ceiling matches the reference. */
    if (!wait_phy_link(3000)) {
        LOG_ERROR("net: PHY link timeout (cable unplugged or PHY fault?)");
        return false;
    }
    LOG_INFO("net: PHY link up");

    /* Apply network info. The ioLibrary expects mutable wiz_NetInfo;
     * we copy into a local because the API isn't const-correct. */
    wiz_NetInfo ni = (wiz_NetInfo){ 0 };
    memcpy(ni.mac, mac,     6);
    memcpy(ni.ip,  ip,      4);
    memcpy(ni.sn,  netmask, 4);
    memcpy(ni.gw,  gateway, 4);
    ni.dhcp = NETINFO_STATIC;
    ctlnetwork(CN_SET_NETINFO, &ni);

    /* 2 KB per socket, both RX and TX — matches the reference. */
    uint8_t memsize[_WIZCHIP_SOCK_NUM_] = { 2, 2, 2, 2, 2, 2, 2, 2 };
    if (wizchip_init(memsize, memsize) != 0) {
        LOG_ERROR("net: wizchip_init failed (socket buffer sizing)");
        return false;
    }

    LOG_INFO("net: ip=%u.%u.%u.%u mask=%u.%u.%u.%u gw=%u.%u.%u.%u",
             ip[0], ip[1], ip[2], ip[3],
             netmask[0], netmask[1], netmask[2], netmask[3],
             gateway[0], gateway[1], gateway[2], gateway[3]);

    s_initialised = true;
    return true;
}

bool net_link_up(void)
{
    if (!s_initialised) return false;
    return wizphy_getphylink() == PHY_LINK_ON;
}

/*----------------------------------------------------------------------------
 * Open / close
 *---------------------------------------------------------------------------*/

bool net_open(net_sock_t sock, net_proto_t proto, uint16_t local_port, bool listen)
{
    if (!s_initialised || !valid_sock(sock)) return false;

    uint8_t mode  = (proto == NET_PROTO_TCP) ? Sn_MR_TCP4 : Sn_MR_UDP4;
    uint8_t flags = 0;

    /* socket() returns the socket number on success, or a negative ioLibrary
     * error code (e.g. SOCKERR_SOCKMODE). It also moves the socket into
     * Sn_MR_INIT and binds local_port. */
    int8_t rc = socket((uint8_t)sock, mode, local_port, flags);
    if (rc != sock) {
        LOG_ERROR("net: socket(%d) open failed rc=%d proto=%d port=%u",
                  (int)sock, (int)rc, (int)proto, (unsigned)local_port);
        return false;
    }

    if (proto == NET_PROTO_TCP && listen) {
        /* socket.h declares `listen(uint8_t sn)` — this shadows POSIX listen
         * but we're not linking against any POSIX networking, so the
         * unqualified call resolves to the ioLibrary one. */
        rc = (int8_t)listen((uint8_t)sock);
        if (rc != SOCK_OK) {
            LOG_ERROR("net: listen(%d) failed rc=%d", (int)sock, (int)rc);
            (void)close((uint8_t)sock);
            return false;
        }
    }

    return true;
}

void net_close(net_sock_t sock)
{
    if (!valid_sock(sock)) return;
    (void)close((uint8_t)sock);
}

/*----------------------------------------------------------------------------
 * TCP
 *---------------------------------------------------------------------------*/

net_tcp_state_t net_tcp_state(net_sock_t sock)
{
    if (!valid_sock(sock)) return NET_TCP_CLOSED;
    return map_sn_sr(getSn_SR((uint8_t)sock));
}

bool net_tcp_connect(net_sock_t sock, const net_addr_t *peer)
{
    (void)sock; (void)peer;
    /* Phase 1 — outbound connect. Not used by Phase 0b. */
    return false;
}

bool net_tcp_reopen_listen(net_sock_t sock, uint16_t local_port)
{
    if (!valid_sock(sock)) return false;
    (void)close((uint8_t)sock);
    return net_open(sock, NET_PROTO_TCP, local_port, true);
}

/*----------------------------------------------------------------------------
 * I/O
 *---------------------------------------------------------------------------*/

int32_t net_send(net_sock_t sock, const uint8_t *buf, size_t len)
{
    if (!valid_sock(sock) || !buf || len == 0) return 0;
    /* The ioLibrary send() blocks until either all bytes are queued in the
     * W6100 TX buffer or an error occurs. For TCP it returns the number
     * sent or a negative error. We treat any negative as a hard fault for
     * the caller to handle (typically by closing). */
    int32_t rc = send((uint8_t)sock, (uint8_t *)buf, (uint16_t)len);
    if (rc <= 0) return rc;
    return rc;
}

int32_t net_recv(net_sock_t sock, uint8_t *buf, size_t maxlen)
{
    if (!valid_sock(sock) || !buf || maxlen == 0) return 0;

    /* recv() blocks if nothing is available. Peek the RX byte count first
     * so we can return 0 (no data) without blocking. */
    uint16_t avail = getSn_RX_RSR((uint8_t)sock);
    if (avail == 0) return 0;

    uint16_t want = (avail < maxlen) ? avail : (uint16_t)maxlen;
    return (int32_t)recv((uint8_t)sock, buf, want);
}

int32_t net_sendto(net_sock_t sock, const net_addr_t *peer,
                   const uint8_t *buf, size_t len)
{
    (void)sock; (void)peer; (void)buf; (void)len;
    return -1;       /* Phase 1 */
}

int32_t net_recvfrom(net_sock_t sock, net_addr_t *peer,
                     uint8_t *buf, size_t maxlen)
{
    (void)sock; (void)peer; (void)buf; (void)maxlen;
    return 0;        /* Phase 1 */
}
