"""Headless smoke test: parser, protocol round-trip, telemetry unpack, GUI build.

Run from the gui/ folder:  python smoke_test.py
Uses Qt's offscreen platform so it needs no display.
"""
import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mc_gui import od as odmod
from mc_gui import protocol as proto


def test_od_parse():
    model = odmod.parse_od_header()
    assert model.get(0x6041, 0).name == "statusword"
    assert model.get(0x6041, 0).type_code == odmod.T_U16
    vk = model.by_name["vel_kp"]
    assert vk.is_float and vk.access == odmod.A_RW and vk.is_persist
    pa = model.by_name["position_actual"]
    assert pa.scaled and abs(pa.scale - 1e-5) < 1e-12 and pa.unit == "rad"
    assert pa.is_pdo
    # synthetic 0x2A00 array members exist
    assert model.get(0x2A00, 1) is not None and model.get(0x2A00, 16) is not None
    # value conversion round-trip on a scaled int
    raw = pa.si_to_raw(1.23456)
    assert abs(pa.raw_to_si(raw) - 1.23456) < 1e-4
    # float entry encode/decode
    assert abs(vk.decode(vk.encode(3.5)) - 3.5) < 1e-6
    print(f"OK od_parse: {len(model.entries)} entries, {len(model.pdo_entries)} PDO, "
          f"source={model.source.name}")
    return model


def test_protocol_roundtrip():
    req = proto.build_read_req(7, 0x6041, 0, odmod.T_U16)
    hdr = proto.parse_header(req)
    assert hdr and hdr.type == proto.MSG_OD_READ_REQ and hdr.seq == 7
    idx, sub, etype = struct.unpack_from("<HBB", req, proto.HEADER_SIZE)
    assert (idx, sub, etype) == (0x6041, 0, odmod.T_U16)

    wr = proto.build_write_req(9, 0x2300, 1, odmod.T_F32, struct.pack("<f", 2.5))
    h2 = proto.parse_header(wr)
    assert h2.type == proto.MSG_OD_WRITE_REQ and h2.seq == 9

    # a read response payload
    resp_payload = struct.pack("<HBBBB", 0x6041, 0, odmod.T_U16, proto.OD_OK, 2) + struct.pack("<H", 0x1234)
    rr = proto.parse_read_resp(resp_payload)
    assert rr.index == 0x6041 and rr.result == proto.OD_OK and rr.data == b"\x34\x12"
    print("OK protocol_roundtrip")


def test_telemetry_unpack(model):
    from mc_gui.client import NetworkClient
    client = NetworkClient(model)
    iq = model.by_name["tlm_iq_meas_a"]
    vel = model.by_name["velocity_actual"]
    client._active_map = [iq, vel]  # iq=float32 (4B), vel=I32 scaled (4B) -> 8B blob

    # build one telemetry record: status header (12B) + blob (8B)
    blob = struct.pack("<f", 1.5) + struct.pack("<i", vel.si_to_raw(2.0))
    sh = struct.pack("<HbBHBBI",
                     0x0037,   # statusword
                     3,        # mode_display
                     3,        # node_state RUNNING
                     0,        # error_code
                     5,        # map_version
                     len(blob),
                     42)       # status_counter
    record = sh + blob
    tlm_payload = struct.pack("<BBBB", 5, 1, len(record), 0) + record
    datagram = proto.pack_header(proto.MSG_TELEMETRY, 1, tlm_payload)

    hdr = proto.parse_header(datagram)
    dg = proto.parse_telemetry(datagram[proto.HEADER_SIZE:proto.HEADER_SIZE + hdr.length])
    samples = client._unpack(dg)
    assert len(samples) == 1
    s = samples[0]
    assert s.layout_ok and s.node_state == 3 and s.counter == 42
    assert abs(s.values["tlm_iq_meas_a"] - 1.5) < 1e-6
    assert abs(s.values["velocity_actual"] - 2.0) < 1e-3
    assert s.values["statusword"] == float(0x0037)
    print("OK telemetry_unpack")


def test_gui_build(model):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    from mc_gui.main_window import MainWindow
    from mc_gui.graph_window import GraphWindow
    win = MainWindow(model)
    assert win.tree.topLevelItemCount() > 0
    win._load_default_map()
    assert len(win._selected_map_entries()) > 0
    gw = GraphWindow(win.buffer, ["statusword", "tlm_iq_meas_a"], initial=["tlm_iq_meas_a"])
    assert "tlm_iq_meas_a" in gw.curves
    win.close()
    gw.close()
    print("OK gui_build (offscreen)")


if __name__ == "__main__":
    m = test_od_parse()
    test_protocol_roundtrip()
    test_telemetry_unpack(m)
    test_gui_build(m)
    print("\nALL SMOKE TESTS PASSED")
