# app/controller_mgr

## Purpose
Per-controller lifecycle. Owns the UDP poll listener and the per-controller TCP send/receive. Dispatches incoming CAMERAD messages to handlers that mutate `cmc_state` and generate responses.

## Owns
- One UDP listen socket for poll (default port 30002).
- Up to 2 controller records (`CMC_MAX_CONTROLLERS = 2`, limited by W6100 socket budget after the two OD UDP ports + log + HTTP — see `Documentation/architecture.md §10.1`), each with:
  - device number, device type, return IP/port
  - last-poll timestamp
  - one outbound TCP socket (CMC opens to controller's `return_port`)
  - one inbound TCP socket (controller opens to CMC's listen port) — preferred for sends if connected
  - per-controller receive buffer (256 bytes)
- The response-shape decision (S vs T body), based on the controller's `device_type`.
- The 5-second inactivity timeout (matches Main CMC `uiTimeout`).

## Does NOT do
- Parse or build CAMERAD bytes — `camerad` does that.
- Hold CMC selection / status / shot state — `cmc_state` does that.
- Touch motors directly — `motor_ctrl` does that, called from `cmc_state` on its behalf.

## Public API
```c
void           ctrl_mgr_init(void);
void           ctrl_mgr_tick(void);          // poll sockets, handle inbound, send pending
size_t         ctrl_mgr_connected_count(void);
```

Internal: per-message handlers (`handle_poll`, `handle_select`, `handle_movement`, ...) that read the parsed message, mutate `cmc_state` via its API, and emit responses by building bytes through `camerad` and sending through `bsp/net`.

## Dependencies
- `camerad` (codec)
- `cmc_state` (state mutations)
- `bsp/net` (sockets)
- `bsp/time` (timeouts, timestamps)

## Acceptance criteria
- Real S- and T-panels can poll, select, deselect, grab, send keypresses, and receive correctly-shaped responses (S-shape to S, T-shape to T) — regardless of which key code was sent.
- Response `message_id` echoes the request's.
- A controller that stops polling for >5 seconds is removed and its socket closed.
- A select request from controller B while A holds selection is denied; the response carries A's controller number.
- Inbound TCP from a controller is accepted and used for responses in preference to opening a new outbound socket (matches Main CMC `REDUCE_TCP_SOCK` behaviour).

## Notes
- Response shape is determined by `device_type`, **never** by the inbound key code. (Reduced-CMC bug to avoid.)
- A POLL may trigger up to three back-to-back responses (poll body + optional CCU status + optional ToF status) per Main CMC. This CMC only sends the poll body — CCU/ToF are out of scope.
- If a controller's IP changes between polls, close the existing TCP socket before updating the record (Reduced-CMC bug to avoid).
