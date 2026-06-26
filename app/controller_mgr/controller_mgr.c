/*
 * app/controller_mgr — Path A (final): structure copied from the Reduced
 * CMC (uc_camd_interface).
 *
 * Three sockets active:
 *   - UDP listen socket on udp_poll_port (30002) — receives POLL discovery
 *     broadcasts from controllers. (app_init.c:97-103)
 *   - TCP listen socket on tcp_camerad_port (30001 — SW050 LISTENPORT1) —
 *     accepts inbound TCP from the controller. The panel uses this for
 *     command messages (SELECT / KEYPRESS / MOVEMENT). (app_init.c:106-112)
 *   - One outbound TCP socket per controller — opened on first POLL,
 *     connects from CMC to controller's return_port (typically 30001 on
 *     the panel side). All responses (POLL response + others) flow over
 *     this socket. (command_handler.c::ensure_controller_connection)
 *
 * Sequence (mirroring command_handler.c::cmd_handle_poll line 110-154):
 *   1. POLL UDP arrives.
 *   2. Extract controller IP + return_port from POLL header.
 *   3. ensure_controller_connection:
 *      - If no outbound socket yet, open one (TCP, ephemeral local port).
 *      - Issue connect to controller's return_port.
 *      - Wait for ESTABLISHED (poll-based, non-blocking on our side).
 *   4. Once connected, send POLL response over the outbound TCP.
 *   5. (Inbound TCP listener stays up so panel can push commands —
 *      Phase B will dispatch those.)
 */

#include "controller_mgr.h"

#include "app/camerad/camerad.h"
#include "app/cmc_state/cmc_state.h"
#include "app/config/config.h"
#include "app/log/log.h"
#include "bsp/net/net.h"
#include "bsp/time/time.h"

#include <string.h>

/* Socket allocation per architecture.md §10.1:
 *   0 = CAMERAD POLL UDP listener
 *   1 = CAMERAD TCP inbound listener (panel-initiated TCP for commands)
 *   2 = Controller A outbound TCP (CMC-initiated, for responses)
 *
 * Phase B adds:
 *   3 = Controller B outbound TCP */
#define POLL_SOCKET         ((net_sock_t)0)
#define TCP_LISTEN_SOCKET   ((net_sock_t)1)
#define CTRL_A_TCP_SOCKET   ((net_sock_t)2)

#define RX_BUF_SIZE         CAMERAD_MAX_FRAME_SIZE

/* TCP listener has its own larger rx buffer + persistent-state byte count
 * so it can implement a proper streaming parser (TCP doesn't guarantee
 * frames arrive whole — bytes can split arbitrarily across reads). 1 KB
 * fits ~9 maximally-sized CAMERAD frames, plenty of slack for any
 * realistic panel TX rate. */
#define TCP_RX_BUF_SIZE     1024u
#define TX_BUF_SIZE         128u
#define CONNECT_TIMEOUT_MS  2000u
/* Ephemeral local port for the outbound TCP slot — predictable per slot
 * (matches Reduced CMC pal_net.c:337 `ephemeral_port = 10000 + sock`). */
#define OUTBOUND_LOCAL_PORT (10000u + (uint16_t)CTRL_A_TCP_SOCKET)

/*----------------------------------------------------------------------------
 * Per-controller record (Path A: one slot)
 *---------------------------------------------------------------------------*/

typedef enum {
    CONN_IDLE = 0,        /* outbound socket closed; nothing pending */
    CONN_CONNECTING,      /* connect() issued; waiting for ESTABLISHED */
    CONN_ESTABLISHED,
} conn_state_t;

typedef struct {
    bool             in_use;
    uint32_t         device_no;
    uint32_t         device_type;
    char             ip_str[16];
    uint16_t         return_port;
    conn_state_t     conn;
    uint32_t         connect_start_ms;
    uint32_t         last_poll_ms;            /* heartbeat for timeout */
    bool             pending_poll_response;
    camerad_header_t pending_poll_request;
} controller_t;

/* If a controller stops POLLing for this long, the CMC considers it gone:
 * outbound TCP closed, slot dropped, and if it owned the camera the
 * selection is force-cleared. Matches Reduced CMC's PROTOCOL_TIMEOUT_MS.
 * Panels normally POLL at ~1 Hz; 5 missed POLLs is unambiguously "gone". */
#define CONTROLLER_TIMEOUT_MS  5000u

static controller_t s_ctrl_a;

static bool                     s_inited = false;
static controller_mgr_stats_t   s_stats;
static uint8_t                  s_rx_buf[RX_BUF_SIZE];

/* TCP listener streaming state — see TCP_RX_BUF_SIZE comment above.
 * s_tcp_rx_pending counts the bytes currently held in s_tcp_rx_buf that
 * we haven't yet been able to consume as complete CAMERAD frames. Each
 * service_tcp_listener tick appends new bytes after the held tail and
 * processes as many full frames as possible before memmove-compacting
 * the leftover back to the buffer start. */
static uint8_t                  s_tcp_rx_buf[TCP_RX_BUF_SIZE];
static size_t                   s_tcp_rx_pending;
static uint8_t                  s_tx_buf[TX_BUF_SIZE];

/* Edge-detect for the TCP listener so the log shows up/down only on change. */
static bool s_listener_up_prev;

/*----------------------------------------------------------------------------
 * Helpers
 *---------------------------------------------------------------------------*/

static void format_our_ip(char out[16])
{
    const network_cfg_t *cfg = config_get_network();
    camerad_format_ip(cfg->ip, out);
}

/*----------------------------------------------------------------------------
 * Response builders.
 *
 * Reused for POLL response, SELECT response, GRAB response — all three are
 * the same body shape (S 22 B / T 14 B) per SW050's eCMCMessageLengths;
 * only the response opcode (echoed in the header's msg_command) differs.
 * Body fields come from app/cmc_state so the panel sees real selection
 * state across responses.
 *
 * DESELECT response is a different shape (5 B body) — separate builder.
 *
 * Advertise tcp_camerad_port as return_port (matches Reduced CMC). The
 * outbound TCP we initiate (CMC → panel's return_port) carries the response.
 *---------------------------------------------------------------------------*/

/* Build a POLL-shaped response body (S 22 B / T 14 B). Returns total frame
 * size written into out; 0 if the device type isn't a recognised
 * controller. msg_command is the opcode to echo (POLL, SELECT, GRAB). */
static uint16_t build_pollshape_response(const camerad_header_t *req,
                                         uint32_t msg_command,
                                         uint8_t *out)
{
    char our_ip[16];
    format_our_ip(our_ip);
    const network_cfg_t *cfg = config_get_network();

    /* Body fields shared between S and T. All come from cmc_state so the
     * panel sees a coherent live picture: who selected the camera, which
     * shot is current/next, whether the motor is moving, etc. */
    uint8_t  camera_selected = cmc_state_is_selected() ? 1u : 0u;
    int32_t  controller_no   = (int32_t)cmc_state_selected_by();
    uint32_t current_shot    = cmc_state_current_shot();
    uint32_t next_shot       = cmc_state_next_shot();
    uint32_t ttshot_tenths   = cmc_state_time_to_shot_tenths();

    /* Two separate status fields per SW050 (CameraToolsU.h):
     *
     * `camera_status` (eCamStatus) — per-camera/motion state. cmsMoving=0x01,
     * cmsOnShot=0x02 are the bits panels render as "moving"/"on shot"
     * indicators. The Reduced CMC's cmc_state.c:228 confusingly wrote
     * these into the cmc_status field at 0x02/0x04 — that's NOT what
     * panels actually read; SW050 panels look in camera_status.
     *
     * `cmc_status` (eCMCStatus) — CMC-mode state. csRemote=0x00 (we are
     * a network-only CMC, never local), csConnectionOK=0x20 set when a
     * controller is selected. */
    uint16_t camera_status = 0;
    if (cmc_state_moving())  camera_status |= CAMERAD_CAM_MOVING;
    if (cmc_state_on_shot()) camera_status |= CAMERAD_CAM_ON_SHOT;

    uint16_t cmc_status = 0;
    if (cmc_state_is_selected()) cmc_status |= CAMERAD_CMC_CONNECTION_OK;

    /* move_type the panel renders as the action icon. CUT shows "instant",
     * FADE shows "fading", NONE = idle. We don't track CUT-vs-FADE
     * separately yet (cmc_state could expose this later); for now
     * report FADING during any active move, NONE when idle. Good enough
     * for the UI to flip the indicator. */
    uint8_t move_type = cmc_state_moving() ? (uint8_t)CAMERAD_MOVE_FADE
                                           : (uint8_t)CAMERAD_MOVE_NONE;

    if (camerad_dev_is_s_type(req->return_device)) {
        camerad_build_response_header(
            out, req, msg_command,
            (uint16_t)sizeof(camerad_poll_resp_s_t),
            our_ip, cfg->tcp_camerad_port, cfg->cmc_device_no);

        camerad_poll_resp_s_t body;
        memset(&body, 0, sizeof(body));
        body.camera_selected = camera_selected;
        body.controller_no   = controller_no;
        body.camera_status   = camera_status;
        body.cmc_status      = cmc_status;
        body.time_to_shot    = (int32_t)ttshot_tenths;
        body.shot_no         = (int32_t)current_shot;
        body.next_shot_no    = (int32_t)next_shot;
        body.move_type       = move_type;
        memcpy(out + CAMERAD_HEADER_SIZE, &body, sizeof(body));
        return (uint16_t)(CAMERAD_HEADER_SIZE + sizeof(body));

    } else if (camerad_dev_is_t_type(req->return_device)) {
        camerad_build_response_header(
            out, req, msg_command,
            (uint16_t)sizeof(camerad_poll_resp_t_t),
            our_ip, cfg->tcp_camerad_port, cfg->cmc_device_no);

        camerad_poll_resp_t_t body;
        memset(&body, 0, sizeof(body));
        body.camera_selected = camera_selected;
        body.controller_no   = controller_no;
        body.camera_status   = camera_status;
        body.cmc_status      = cmc_status;
        body.time_to_shot    = (int32_t)ttshot_tenths;
        body.move_type       = move_type;
        memcpy(out + CAMERAD_HEADER_SIZE, &body, sizeof(body));
        return (uint16_t)(CAMERAD_HEADER_SIZE + sizeof(body));

    } else {
        s_stats.poll_rejected_dev++;
        return 0;
    }
}

/* DESELECT response — 5-byte body: camera_selected, controller_no.
 * Sent whether the deselect was granted or denied. If granted,
 * camera_selected=false; if denied (someone else owns), camera_selected=true
 * and controller_no = whoever currently owns. (Mirrors Reduced CMC
 * command_handler.c::cmd_send_deselect_response.) */
static uint16_t build_deselect_response(const camerad_header_t *req,
                                        uint8_t *out)
{
    char our_ip[16];
    format_our_ip(our_ip);
    const network_cfg_t *cfg = config_get_network();

    camerad_build_response_header(
        out, req, (uint32_t)CAMERAD_MSG_DESELECT,
        (uint16_t)sizeof(camerad_deselect_resp_t),
        our_ip, cfg->tcp_camerad_port, cfg->cmc_device_no);

    camerad_deselect_resp_t body;
    body.camera_selected = cmc_state_is_selected() ? 1u : 0u;
    body.controller_no   = (int32_t)cmc_state_selected_by();
    memcpy(out + CAMERAD_HEADER_SIZE, &body, sizeof(body));
    return (uint16_t)(CAMERAD_HEADER_SIZE + sizeof(body));
}

/* Backwards-compat wrapper for the outbound TCP path that always sends
 * a POLL-shaped response with command=POLL. */
static uint16_t build_poll_response(const camerad_header_t *req)
{
    if (camerad_dev_is_s_type(req->return_device)) s_stats.poll_responded_s++;
    else if (camerad_dev_is_t_type(req->return_device)) s_stats.poll_responded_t++;
    return build_pollshape_response(req, (uint32_t)CAMERAD_MSG_POLL, s_tx_buf);
}

/*----------------------------------------------------------------------------
 * Outbound TCP connection per controller
 *
 * Mirrors command_handler.c::ensure_controller_connection (line 49-87).
 *---------------------------------------------------------------------------*/

static void start_outbound_tcp(controller_t *c)
{
    /* Always close-and-reopen — TCP socket must be in CLOSED state. */
    net_close(CTRL_A_TCP_SOCKET);

    if (!net_open(CTRL_A_TCP_SOCKET, NET_PROTO_TCP, OUTBOUND_LOCAL_PORT, false)) {
        LOG_WARN("ctrl_mgr: net_open(TCP outbound) failed");
        c->conn = CONN_IDLE;
        return;
    }

    net_addr_t peer;
    memset(&peer, 0, sizeof(peer));
    if (!camerad_parse_ip(c->ip_str, peer.addr)) {
        LOG_WARN("ctrl_mgr: unparseable controller IP '%s'", c->ip_str);
        net_close(CTRL_A_TCP_SOCKET);
        c->conn = CONN_IDLE;
        return;
    }
    peer.port = c->return_port;

    if (!net_tcp_connect(CTRL_A_TCP_SOCKET, &peer)) {
        LOG_WARN("ctrl_mgr: net_tcp_connect %s:%u failed",
                 c->ip_str, (unsigned)c->return_port);
        net_close(CTRL_A_TCP_SOCKET);
        c->conn = CONN_IDLE;
        return;
    }

    c->conn             = CONN_CONNECTING;
    c->connect_start_ms = time_ms();
    s_stats.tcp_outbound_connects++;
    LOG_INFO("ctrl_mgr: TCP outbound connect started -> %s:%u (dev=%lu type=%lu)",
             c->ip_str, (unsigned)c->return_port,
             (unsigned long)c->device_no, (unsigned long)c->device_type);
}

static void service_outbound_tcp(controller_t *c)
{
    if (!c->in_use) return;

    net_tcp_state_t st = net_tcp_state(CTRL_A_TCP_SOCKET);

    if (c->conn == CONN_CONNECTING) {
        if (st == NET_TCP_ESTABLISHED) {
            c->conn = CONN_ESTABLISHED;
            LOG_INFO("ctrl_mgr: TCP outbound established -> %s:%u",
                     c->ip_str, (unsigned)c->return_port);
        } else if (time_elapsed_ms(c->connect_start_ms) > CONNECT_TIMEOUT_MS) {
            LOG_WARN("ctrl_mgr: TCP outbound connect timeout -> %s:%u",
                     c->ip_str, (unsigned)c->return_port);
            s_stats.tcp_outbound_failures++;
            net_close(CTRL_A_TCP_SOCKET);
            c->conn = CONN_IDLE;
        }
    }

    if (c->conn == CONN_ESTABLISHED && c->pending_poll_response) {
        uint16_t len = build_poll_response(&c->pending_poll_request);
        if (len > 0) {
            int32_t sent = net_send(CTRL_A_TCP_SOCKET, s_tx_buf, len);
            if (sent < 0 || (uint32_t)sent != len) {
                LOG_WARN("ctrl_mgr: outbound TCP send failed (rc=%ld)", (long)sent);
                s_stats.poll_send_errors++;
                net_close(CTRL_A_TCP_SOCKET);
                c->conn = CONN_IDLE;
            } else {
                /* POLL response sent OK — bump per-opcode counter so it
                 * mirrors the other handlers (handle_select/grab/etc. all
                 * go through send_response_via_outbound which does this
                 * automatically). Without this, /api/stats reports
                 * tx_ok[POLL]=0 even when responses are flowing. */
                s_stats.tx_ok[CAMERAD_MSG_POLL]++;
            }
        }
        c->pending_poll_response = false;
    }

    if ((c->conn == CONN_ESTABLISHED) &&
        (st == NET_TCP_CLOSED || st == NET_TCP_CLOSE_WAIT)) {
        LOG_INFO("ctrl_mgr: TCP outbound closed by peer -> %s:%u",
                 c->ip_str, (unsigned)c->return_port);
        net_close(CTRL_A_TCP_SOCKET);
        c->conn = CONN_IDLE;
    }
}

/*----------------------------------------------------------------------------
 * Inbound TCP command dispatch
 *
 * The panel sends SELECT/DESELECT/GRAB (and later KEYPRESS/MOVEMENT/etc.)
 * over the inbound TCP. Responses go back over the outbound TCP that
 * controller_mgr opens to the panel — that's how the Reduced CMC works
 * (cmd_send_poll_response uses controller->send_socket, the outbound).
 *
 * For Path A we only handle SELECT/DESELECT/GRAB — enough to keep the
 * panel UI green. Other commands are silently dropped until Phase B.
 *---------------------------------------------------------------------------*/

/* Send a response that's already in s_tx_buf via the outbound TCP. If the
 * outbound isn't ESTABLISHED yet, the response is dropped (panel will
 * retry; or its next POLL will trigger reconnection). `opcode` is the
 * CAMERAD msg_command of the response — used purely for tx_ok[] stats. */
static void send_response_via_outbound(uint16_t len, uint32_t opcode)
{
    if (!s_ctrl_a.in_use || s_ctrl_a.conn != CONN_ESTABLISHED) return;
    int32_t sent = net_send(CTRL_A_TCP_SOCKET, s_tx_buf, len);
    if (sent < 0 || (uint32_t)sent != len) {
        LOG_WARN("ctrl_mgr: response TCP send failed (rc=%ld)", (long)sent);
        s_stats.poll_send_errors++;
    } else if (opcode < CTRL_MGR_OPCODES) {
        s_stats.tx_ok[opcode]++;
    }
}

static void handle_select(const camerad_header_t *req)
{
    uint32_t requester = req->return_device_no;
    bool granted = cmc_state_handle_select(requester);
    uint16_t len;
    uint32_t resp_op;
    if (granted) {
        /* SELECT granted — respond with POLL-shaped body, command echoed
         * as SELECT, camera_selected=true, controller_no=requester. */
        len = build_pollshape_response(req, (uint32_t)CAMERAD_MSG_SELECT, s_tx_buf);
        resp_op = (uint32_t)CAMERAD_MSG_SELECT;
    } else {
        /* SELECT denied — respond with DESELECT-shaped body carrying the
         * current owner's controller_no (mirrors Reduced CMC pattern). */
        len = build_deselect_response(req, s_tx_buf);
        resp_op = (uint32_t)CAMERAD_MSG_DESELECT;
    }
    if (len > 0) send_response_via_outbound(len, resp_op);
}

static void handle_deselect(const camerad_header_t *req)
{
    (void)cmc_state_handle_deselect(req->return_device_no);
    uint16_t len = build_deselect_response(req, s_tx_buf);
    if (len > 0) send_response_via_outbound(len, (uint32_t)CAMERAD_MSG_DESELECT);
}

static void handle_grab(const camerad_header_t *req)
{
    cmc_state_handle_grab(req->return_device_no);
    /* GRAB always grants — respond with POLL-shaped body, command=GRAB. */
    uint16_t len = build_pollshape_response(req, (uint32_t)CAMERAD_MSG_GRAB, s_tx_buf);
    if (len > 0) send_response_via_outbound(len, (uint32_t)CAMERAD_MSG_GRAB);
}

/*----------------------------------------------------------------------------
 * KEYPRESS dispatch
 *
 * KEYPRESS_T1 body = 5 bytes (camerad_keypress_t1_t): {u8 key_code, i32 value}.
 *   For shot keys, `value` is the 1-based shot_no.
 *   For STORE_TIME_TO_SHOT, `value` is the fade time in tenths of a second.
 *
 * KEYPRESS_T2 body = 2 bytes (camerad_keypress_t2_t): {u8 key_code, u8 status}.
 *   Used for STOP / STOP_ALL and similar zero-argument actions.
 *
 * After dispatching the action to cmc_state, we send back a POLL-shaped
 * response carrying the live state — that's what the Reduced CMC does for
 * KEYPRESS responses (see uc_camd_interface command_handler.c::
 * cmd_send_keypress1_response). The panel uses it to update its UI:
 *   - camera_selected / controller_no — unchanged (operator owns it)
 *   - cmc_status bits — moving/on_shot flip as the move progresses
 *   - current_shot / next_shot / time_to_shot — reflect the just-issued
 *     action immediately, so the panel can display "going to shot 5".
 *---------------------------------------------------------------------------*/

static void send_keypress_response(const camerad_header_t *req, uint32_t echoed_cmd)
{
    uint16_t len = build_pollshape_response(req, echoed_cmd, s_tx_buf);
    if (len > 0) send_response_via_outbound(len, echoed_cmd);
}

/* Human-readable CAMERAD key-code name for the dispatch log. Only the
 * codes we actually handle are listed; everything else shows as "?". */
static const char *camerad_kc_name(uint8_t kc)
{
    switch ((camerad_key_code_t)kc) {
    case CAMERAD_KC_STORE_SHOT:         return "STORE_SHOT";
    case CAMERAD_KC_STORE_NEXT:         return "STORE_NEXT";
    case CAMERAD_KC_SWOOP:              return "SWOOP";
    case CAMERAD_KC_CUT:                return "CUT";
    case CAMERAD_KC_FADE:               return "FADE";
    case CAMERAD_KC_FADE_CUE:           return "FADE_CUE";
    case CAMERAD_KC_CUT_CUE:            return "CUT_CUE";
    case CAMERAD_KC_PRELOAD:            return "PRELOAD";
    case CAMERAD_KC_SWOOP_TO:           return "SWOOP_TO";
    case CAMERAD_KC_CUT_TO:             return "CUT_TO";
    case CAMERAD_KC_FADE_TO:            return "FADE_TO";
    case CAMERAD_KC_JOY_PROFILE_NORMAL: return "JOY_PROFILE_NORMAL";
    case CAMERAD_KC_JOY_PROFILE_MEDIUM: return "JOY_PROFILE_MEDIUM";
    case CAMERAD_KC_JOY_PROFILE_FINE:   return "JOY_PROFILE_FINE";
    case CAMERAD_KC_STORE_TIME_TO_SHOT: return "STORE_TIME_TO_SHOT";
    case CAMERAD_KC_STOP:               return "STOP";
    case CAMERAD_KC_STOP_ALL:           return "STOP_ALL";
    default:                            return "?";
    }
}

static void handle_keypress_t1(const camerad_header_t *req,
                               const uint8_t *body, size_t body_len)
{
    if (body_len < sizeof(camerad_keypress_t1_t)) {
        LOG_WARN("ctrl_mgr: KEYPRESS_T1 body too short (%u)", (unsigned)body_len);
        return;
    }
    camerad_keypress_t1_t kp;
    /* Body is packed LE so a direct memcpy is correct on Cortex-M. */
    memcpy(&kp, body, sizeof(kp));

    LOG_INFO("ctrl_mgr: KEYPRESS_T1 key=%s (0x%02X) value=%ld",
             camerad_kc_name(kp.key_code), (unsigned)kp.key_code, (long)kp.value);

    switch ((camerad_key_code_t)kp.key_code) {
    case CAMERAD_KC_STORE_SHOT:
        (void)cmc_state_store_shot((uint32_t)kp.value);
        break;
    case CAMERAD_KC_STORE_NEXT:
        (void)cmc_state_store_next();
        break;

    case CAMERAD_KC_CUT:
    case CAMERAD_KC_CUT_TO:
    case CAMERAD_KC_CUT_CUE:
        (void)cmc_state_move_to_shot((uint32_t)kp.value, /*is_cut*/true);
        break;

    /* FADE and SWOOP share the same dispatch: a timed move to the shot's
     * stored position, using the operator-locked time_to_shot (or the
     * shot's stored time if none locked). Per the v1 decision the SWOOP
     * "custom curve" profile is NOT implemented — the panel still sends
     * the SWOOP key code, we just execute a plain FADE. The panel sees
     * a fading-indicator either way (POLL response reports
     * CAMERAD_MOVE_FADE for any active move). */
    case CAMERAD_KC_FADE:
    case CAMERAD_KC_FADE_TO:
    case CAMERAD_KC_FADE_CUE:
    case CAMERAD_KC_SWOOP:
    case CAMERAD_KC_SWOOP_TO:
        (void)cmc_state_move_to_shot((uint32_t)kp.value, /*is_cut*/false);
        break;

    case CAMERAD_KC_STORE_TIME_TO_SHOT:
        cmc_state_set_time_to_shot_tenths((uint32_t)kp.value);
        break;

    case CAMERAD_KC_STOP:
        cmc_state_stop_movement();
        break;

    case CAMERAD_KC_PRELOAD:
        /* T-screen "preload" — operator selected a shot in the UI but
         * hasn't pressed move yet. Reduced CMC just acks; we do the same
         * (no state change, response carries current state). */
        break;

    case CAMERAD_KC_JOY_PROFILE_NORMAL:
    case CAMERAD_KC_JOY_PROFILE_MEDIUM:
    case CAMERAD_KC_JOY_PROFILE_FINE:
        /* Joystick profile — not yet wired into axis_manager.
         * For now, accept and ack. Phase later: map to a joystick
         * acceleration/scaling profile in axis_manager. */
        LOG_INFO("ctrl_mgr: joystick profile request 0x%02X (not yet wired)",
                 (unsigned)kp.key_code);
        break;

    default:
        LOG_INFO("ctrl_mgr: KEYPRESS_T1 unhandled key 0x%02X (ignored, will still ack)",
                 (unsigned)kp.key_code);
        break;
    }

    send_keypress_response(req, (uint32_t)CAMERAD_MSG_KEYPRESS_T1);
}

static void handle_keypress_t2(const camerad_header_t *req,
                               const uint8_t *body, size_t body_len)
{
    if (body_len < sizeof(camerad_keypress_t2_t)) {
        LOG_WARN("ctrl_mgr: KEYPRESS_T2 body too short (%u)", (unsigned)body_len);
        return;
    }
    camerad_keypress_t2_t kp;
    memcpy(&kp, body, sizeof(kp));

    LOG_INFO("ctrl_mgr: KEYPRESS_T2 key=0x%02X status=%u",
             (unsigned)kp.key_code, (unsigned)kp.status);

    switch ((camerad_key_code_t)kp.key_code) {
    case CAMERAD_KC_STOP:
    case CAMERAD_KC_STOP_ALL:
        cmc_state_stop_movement();
        break;
    default:
        /* Limits / toggles / other KP2 keys not handled in v1. */
        break;
    }
    send_keypress_response(req, (uint32_t)CAMERAD_MSG_KEYPRESS_T2);
}

/* MOVEMENT body (9 bytes, camerad_movement_t): {u8 axis_bitmap, i8 pan,
 * i8 tilt, i8 zoom, i8 focus, i8 x, i8 y, i8 height, i8 fader}. Sent
 * by the controller at its joystick rate (~25 ms while the stick is
 * deflected, plus a final zero on release). No response expected
 * (camerad.h:264 — "No ACK from CMC"). */
static void handle_movement(const camerad_header_t *req,
                            const uint8_t *body, size_t body_len)
{
    (void)req;
    if (body_len < sizeof(camerad_movement_t)) {
        LOG_WARN("ctrl_mgr: MOVEMENT body too short (%u)", (unsigned)body_len);
        return;
    }
    camerad_movement_t mv;
    memcpy(&mv, body, sizeof(mv));

    /* Single-motor CMC: only the pan axis is wired. axis_bitmap tells us
     * which fields the panel actually populated; we still accept pan=0
     * frames (operator released the stick) to drive the motor to a stop
     * — that's distinct from "no MOVEMENT at all" (handled by the
     * cmc_state watchdog). */
    cmc_state_handle_movement(mv.pan);
}

/* Human-readable CAMERAD opcode name for the dispatch log. Only the
 * opcodes we actually receive are listed; unknowns show as "?". */
static const char *camerad_msg_name(uint32_t op)
{
    switch ((camerad_msg_t)op) {
    case CAMERAD_MSG_POLL:          return "POLL";
    case CAMERAD_MSG_SELECT:        return "SELECT";
    case CAMERAD_MSG_DESELECT:      return "DESELECT";
    case CAMERAD_MSG_GRAB:          return "GRAB";
    case CAMERAD_MSG_KEYPRESS_T1:   return "KEYPRESS_T1";
    case CAMERAD_MSG_KEYPRESS_T2:   return "KEYPRESS_T2";
    case CAMERAD_MSG_KEYPRESS_T3:   return "KEYPRESS_T3";
    case CAMERAD_MSG_MOVEMENT:      return "MOVEMENT";
    case CAMERAD_MSG_LIMIT:         return "LIMIT";
    case CAMERAD_MSG_POSITION_REQ:  return "POSITION_REQ";
    default:                        return "?";
    }
}

static void handle_command(const camerad_header_t *req,
                           const uint8_t *body, size_t body_len)
{
    /* Stamp rx_ok per opcode before dispatch so the count includes the
     * call even if the handler bails. Unknown opcodes go to the default
     * branch and increment rx_unknown_opcode instead. */
    uint32_t op = req->msg_command;
    if (op < CTRL_MGR_OPCODES) s_stats.rx_ok[op]++;

    /* MOVEMENT arrives at ~25-40 fps even when the stick is centred — too
     * noisy for the dispatch log. Everything else is operator-driven and
     * low-rate; log it so the shot-recall chain is traceable end-to-end. */
    if (op != (uint32_t)CAMERAD_MSG_MOVEMENT) {
        LOG_INFO("ctrl_mgr: RX %s from ctrl=%lu (msg_id=%lu)",
                 camerad_msg_name(op),
                 (unsigned long)req->return_device_no,
                 (unsigned long)req->message_id);
    }

    switch ((camerad_msg_t)op) {
    case CAMERAD_MSG_SELECT:        handle_select     (req);                 break;
    case CAMERAD_MSG_DESELECT:      handle_deselect   (req);                 break;
    case CAMERAD_MSG_GRAB:          handle_grab       (req);                 break;
    case CAMERAD_MSG_KEYPRESS_T1:   handle_keypress_t1(req, body, body_len); break;
    case CAMERAD_MSG_KEYPRESS_T2:   handle_keypress_t2(req, body, body_len); break;
    case CAMERAD_MSG_MOVEMENT:      handle_movement   (req, body, body_len); break;
    /* Phase B+: KEYPRESS_T3, LIMIT, POSITION_REQ, LEARN_ID_REQ — silently
     * dropped. Stats keep the rx_ok[] count for them so a test that
     * sends those opcodes can see they arrived even though we ignore them. */
    default:
        s_stats.rx_unknown_opcode++;
        if (op < CTRL_MGR_OPCODES) s_stats.rx_ok[op]--;  /* undo the speculative bump above */
        break;
    }
}

/*----------------------------------------------------------------------------
 * Inbound TCP listener (panel-initiated TCP for commands)
 *
 * Mirrors Reduced CMC's app_init.c:106-112.
 *---------------------------------------------------------------------------*/

static void open_tcp_listen(void)
{
    const network_cfg_t *cfg = config_get_network();
    if (!net_tcp_reopen_listen(TCP_LISTEN_SOCKET, cfg->tcp_camerad_port)) {
        LOG_ERROR("ctrl_mgr: failed to (re)open TCP listen on %u",
                  (unsigned)cfg->tcp_camerad_port);
    }
}

static void service_tcp_listener(void)
{
    net_tcp_state_t st = net_tcp_state(TCP_LISTEN_SOCKET);
    bool is_up = (st == NET_TCP_ESTABLISHED);

    if (is_up && !s_listener_up_prev) {
        const network_cfg_t *cfg = config_get_network();
        s_stats.tcp_listener_accepts++;
        LOG_INFO("ctrl_mgr: TCP listener inbound up on :%u",
                 (unsigned)cfg->tcp_camerad_port);
    } else if (!is_up && s_listener_up_prev) {
        s_stats.tcp_listener_closes++;
        LOG_INFO("ctrl_mgr: TCP listener inbound down (state=%d)", (int)st);
    }
    s_listener_up_prev = is_up;

    if (st == NET_TCP_CLOSED || st == NET_TCP_CLOSE_WAIT) {
        /* Drop any partial-frame bytes carried from the previous
         * connection. The new client's first bytes must start a fresh
         * frame. */
        s_tcp_rx_pending = 0;
        open_tcp_listen();
        return;
    }

    if (!is_up) return;

    /* Streaming-parser pattern: TCP is a byte stream, so frames can split
     * arbitrarily across reads. We APPEND new bytes after any leftover
     * from the previous tick, parse out as many full CAMERAD frames as
     * we have, then memmove the partial tail back to buffer start for
     * the next tick. Bytes are NEVER discarded due to mid-frame splits;
     * the only thing that flushes the buffer is a magic/length-validity
     * parse failure (real corruption, not a partial). */
    /* Only recv if there's free space in the buffer. Calling net_recv with
     * len=0 is undefined behaviour on W6100 ioLibrary in some versions —
     * can return error / lock the socket. The defensive code at the
     * bottom of this function resets pending if the buffer ever stays
     * full across a tick, so we'll catch up next iteration. */
    if (s_tcp_rx_pending < sizeof(s_tcp_rx_buf)) {
        int32_t n = net_recv(TCP_LISTEN_SOCKET,
                             s_tcp_rx_buf + s_tcp_rx_pending,
                             (int32_t)(sizeof(s_tcp_rx_buf) - s_tcp_rx_pending));
        if (n > 0) {
            s_tcp_rx_pending += (size_t)n;
        }
    }

    if (s_tcp_rx_pending > s_stats.rx_buf_high_water) {
        s_stats.rx_buf_high_water = (uint32_t)s_tcp_rx_pending;
    }

    /* Parse as many full frames as we can. CAMERAD's authoritative size
     * is message_length in the header. */
    size_t offset = 0;
    while (s_tcp_rx_pending - offset >= CAMERAD_HEADER_SIZE) {
        camerad_header_t req;
        if (!camerad_parse_header(s_tcp_rx_buf + offset,
                                  s_tcp_rx_pending - offset, &req)) {
            /* Real corruption (bad magic / invalid length) — not a split.
             * We can't recover by waiting for more bytes; the stream is
             * misaligned. Flush and re-sync; if the peer is well-behaved
             * the next frame they send will magic-match cleanly. */
            s_stats.rx_parse_fail++;
            s_tcp_rx_pending = 0;
            return;
        }
        if (req.message_length > s_tcp_rx_pending - offset) {
            /* Body not all here yet — keep what we have for next tick.
             * This is the normal "TCP delivered a partial" path, NOT an
             * error; don't bump rx_parse_fail. */
            break;
        }

        /* Body starts immediately after the 64-byte header. message_length
         * is the total frame size including the header. */
        const uint8_t *body     = s_tcp_rx_buf + offset + CAMERAD_HEADER_SIZE;
        size_t         body_len = (size_t)req.message_length - CAMERAD_HEADER_SIZE;
        handle_command(&req, body, body_len);
        offset += req.message_length;
    }

    /* Compact: shift any unconsumed bytes (incomplete-frame tail) to the
     * start of the buffer so the next net_recv appends cleanly. */
    if (offset > 0) {
        if (offset < s_tcp_rx_pending) {
            memmove(s_tcp_rx_buf, s_tcp_rx_buf + offset,
                    s_tcp_rx_pending - offset);
        }
        s_tcp_rx_pending -= offset;
    }

    /* Defensive: if a peer sends a sequence that fills the buffer without
     * ever producing a parseable header (pathological — only possible if
     * a 1 KB stream of garbage arrives without a single magic), reset to
     * avoid wedging. The rx_truncated counter shows it happened. */
    if (s_tcp_rx_pending >= sizeof(s_tcp_rx_buf)) {
        s_stats.rx_truncated++;
        s_tcp_rx_pending = 0;
    }
}

/*----------------------------------------------------------------------------
 * Slot management
 *---------------------------------------------------------------------------*/

static controller_t *find_or_assign_slot(const camerad_header_t *req)
{
    if (s_ctrl_a.in_use) {
        if (s_ctrl_a.device_no == req->return_device_no
         && strncmp(s_ctrl_a.ip_str, req->return_address, sizeof(s_ctrl_a.ip_str)) == 0) {
            return &s_ctrl_a;
        }
        net_close(CTRL_A_TCP_SOCKET);
        memset(&s_ctrl_a, 0, sizeof(s_ctrl_a));
    }

    s_ctrl_a.in_use      = true;
    s_ctrl_a.device_no   = req->return_device_no;
    s_ctrl_a.device_type = req->return_device;
    strncpy(s_ctrl_a.ip_str, req->return_address, sizeof(s_ctrl_a.ip_str) - 1);
    s_ctrl_a.ip_str[sizeof(s_ctrl_a.ip_str) - 1] = '\0';
    s_ctrl_a.return_port = (uint16_t)req->return_port;
    s_ctrl_a.conn        = CONN_IDLE;
    return &s_ctrl_a;
}

/*----------------------------------------------------------------------------
 * POLL dispatch
 *---------------------------------------------------------------------------*/

static void handle_poll(const camerad_header_t *req)
{
    controller_t *c = find_or_assign_slot(req);

    c->device_type = req->return_device;
    if (strncmp(c->ip_str, req->return_address, sizeof(c->ip_str)) != 0) {
        strncpy(c->ip_str, req->return_address, sizeof(c->ip_str) - 1);
        c->ip_str[sizeof(c->ip_str) - 1] = '\0';
        if (c->conn != CONN_IDLE) {
            net_close(CTRL_A_TCP_SOCKET);
            c->conn = CONN_IDLE;
        }
    }
    c->return_port = (uint16_t)req->return_port;
    c->last_poll_ms = time_ms();    /* heartbeat — used by tick timeout */

    c->pending_poll_request  = *req;
    c->pending_poll_response = true;

    if (c->conn == CONN_IDLE) {
        start_outbound_tcp(c);
    }
}

/*----------------------------------------------------------------------------
 * UDP POLL socket service
 *---------------------------------------------------------------------------*/

static void service_poll_socket(void)
{
    net_addr_t from;
    int32_t n = net_recvfrom(POLL_SOCKET, &from, s_rx_buf, sizeof(s_rx_buf));
    if (n <= 0) return;

    if ((size_t)n >= sizeof(s_rx_buf)) s_stats.rx_truncated++;

    camerad_header_t req;
    if (!camerad_parse_header(s_rx_buf, (size_t)n, &req)) {
        s_stats.poll_rejected_hdr++;
        s_stats.rx_parse_fail++;
        return;
    }
    if ((int32_t)req.message_length != n) {
        s_stats.poll_rejected_hdr++;
        s_stats.rx_parse_fail++;
        return;
    }
    if ((camerad_msg_t)req.msg_command != CAMERAD_MSG_POLL) return;

    (void)from;
    s_stats.poll_received++;
    s_stats.rx_ok[CAMERAD_MSG_POLL]++;

    /* Per-POLL log removed — was 1 line/sec while connected, which crowds
     * out the request/dispatch chain we actually care about during shot
     * recalls. To verify the panel is alive use either:
     *   - GET /api/stats (rx_ok.POLL counter advances), or
     *   - the controller-timeout log: if POLLs stop, evict_controller fires
     *     within CONTROLLER_TIMEOUT_MS (5 s) and you'll see "controller
     *     timed out — evicting". */
    handle_poll(&req);
}

/*----------------------------------------------------------------------------
 * Lifecycle
 *---------------------------------------------------------------------------*/

void controller_mgr_init(void)
{
    memset(&s_stats,  0, sizeof(s_stats));
    memset(&s_ctrl_a, 0, sizeof(s_ctrl_a));
    s_listener_up_prev = false;
    s_tcp_rx_pending   = 0;

    const network_cfg_t *cfg = config_get_network();
    uint16_t udp_port = (cfg->udp_poll_port    != 0) ? cfg->udp_poll_port    : 30002u;
    uint16_t tcp_port = (cfg->tcp_camerad_port != 0) ? cfg->tcp_camerad_port : 30001u;

    if (!net_open(POLL_SOCKET, NET_PROTO_UDP, udp_port, false)) {
        LOG_ERROR("ctrl_mgr: failed to open POLL UDP %u", (unsigned)udp_port);
        return;
    }

    if (!net_open(TCP_LISTEN_SOCKET, NET_PROTO_TCP, tcp_port, true)) {
        LOG_ERROR("ctrl_mgr: failed to open TCP listen on %u", (unsigned)tcp_port);
    }

    s_inited = true;
    LOG_INFO("ctrl_mgr: ready. POLL UDP=%u, TCP listen=%u (device_no=%lu, type=%u)",
             (unsigned)udp_port, (unsigned)tcp_port,
             (unsigned long)cfg->cmc_device_no,
             (unsigned)CAMERAD_DEV_CMC_BLDC);
}

/* (Periodic stats dump removed — was generating ~12 log lines every 10s
 * and drowning out the event trace. Counters still update internally
 * and are exposed via GET /api/stats for tools that want them.) */

/* Drop the slot + outbound TCP and force-clear any selection this
 * controller held. Called when the heartbeat timeout fires. Mirrors
 * Reduced CMC's cmc_remove_controller(). */
static void evict_controller(controller_t *c)
{
    uint32_t dev = c->device_no;
    LOG_INFO("ctrl_mgr: controller %lu timed out (no POLL for %u ms) — evicting",
             (unsigned long)dev, (unsigned)CONTROLLER_TIMEOUT_MS);
    cmc_state_force_deselect(dev);
    if (c->conn != CONN_IDLE) {
        net_close(CTRL_A_TCP_SOCKET);
    }
    memset(c, 0, sizeof(*c));
}

static void check_controller_timeouts(void)
{
    /* Only one slot in Path A. Iterate when Phase B adds Controller B. */
    if (!s_ctrl_a.in_use) return;
    if (time_elapsed_ms(s_ctrl_a.last_poll_ms) > CONTROLLER_TIMEOUT_MS) {
        evict_controller(&s_ctrl_a);
    }
}

void controller_mgr_tick(void)
{
    if (!s_inited) return;
    service_poll_socket();
    service_outbound_tcp(&s_ctrl_a);
    service_tcp_listener();
    check_controller_timeouts();
    /* (Periodic stats dump removed — counters are still queryable via
     * /api/stats and stay in g_debug for the debugger. Log is now
     * event-driven so the shot-recall trace is the dominant signal.) */
}

/*----------------------------------------------------------------------------
 * Stats
 *---------------------------------------------------------------------------*/

void controller_mgr_get_stats(controller_mgr_stats_t *out)
{
    if (out) *out = s_stats;
}
