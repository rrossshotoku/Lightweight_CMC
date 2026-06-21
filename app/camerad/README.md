# app/camerad

## Purpose
Stateless CAMERAD codec. Parses incoming bytes into typed structs; builds typed structs into outgoing bytes.

## Owns
- The 64-byte CAMD message header layout (parse + build).
- One parser and one builder per body shape this CMC handles (S/T poll responses, movement, keypress 1/2/3, position request, deselect).
- The opcode enum (`mcCommand`), key-code enum (`eKeyCodes`), camera/CMC status bit masks, device-type enum. Values come from SW050 (`Trunk/CMCapp/CameraToolsU.h`), not from the PDF.

## Does NOT do
- Hold any state.
- Open or read from any socket.
- Decide which body shape to send back — that's `controller_mgr`'s job, based on the requesting device type.

## Public API
```c
/* Header */
camerad_status_t camerad_parse_header(const uint8_t *bytes, size_t len, camerad_hdr_t *out);
size_t           camerad_build_header(const camerad_hdr_t *hdr, uint8_t *out);

/* Bodies — one pair per shape */
camerad_status_t camerad_parse_movement(const uint8_t *body, size_t len, camerad_movement_t *out);
size_t camerad_build_poll_response_s(const camerad_poll_resp_s_t *r, uint8_t *out);
size_t camerad_build_poll_response_t(const camerad_poll_resp_t_t *r, uint8_t *out);
/* ...etc, one pair per message family in scope... */
```

All builders return number of bytes written (0 on error). All parsers return a status code.

## Dependencies
None — pure C, types only. Must not include `bsp/` or `Drivers/`.

## Acceptance criteria
- A captured S-panel poll round-trips through `parse_header` → `build_header` byte-identical.
- All opcode and key-code values match the SW050 source for any code referenced by an S- or T-panel.
- Compiles standalone (no link dependencies).
- Unit-testable on the desktop without modification.

## Notes
- Wire byte order is little-endian; structures are byte-packed (`#pragma pack(1)`).
- Magic string is exactly `"CAMERAD"` followed by a NUL — validation is strict on all 8 bytes (don't repeat the Reduced CMC's "memcmp 7 bytes only" mistake).
- Version string the CMC builds: `"1.3"` (matches Main CMC default). Version negotiation is a `controller_mgr` concern, not `camerad`'s.
- `message_length` is the total wire length including the header.
- `message_id` is echoed from the request on every response (`controller_mgr` provides the value to pass through).
- `iPacketID` is always zero on outgoing.
