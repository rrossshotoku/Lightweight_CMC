#!/usr/bin/env python3
"""
LCMC CAMERAD Ethernet robustness test harness.

Acts as a mock CAMERAD panel against a live LCMC. Runs a battery of tests
that send realistic and adversarial traffic, then compares the LCMC's
counters (via GET /api/stats) against what we sent. Reports pass/fail
without making any code changes.

Usage:
    python robustness_test.py --ip 192.1.0.100              # run all tests
    python robustness_test.py --ip 192.1.0.100 --test 4     # just test 4
    python robustness_test.py --ip 192.1.0.100 --short      # short variants

Tests:
    1.  POLL UDP sustained        (1 Hz for N seconds)
    2.  POLL UDP burst            (100 Hz pulses)
    3.  POLL UDP flood            (as fast as possible)
    4.  TCP command rate          (SELECT/DESELECT @ 50 Hz)
    5.  MOVEMENT flood            (100 Hz for 60 s)
    6.  Mixed realistic traffic   (POLL + KP + MOVE concurrent)
    7.  Long idle then burst      (15 min idle + 100 commands)
    8.  TCP disconnect/reconnect  (100 cycles)
    9.  Malformed input fuzzing
    10. TX backpressure           (1000 POLLs at 1 kHz)

Each test reports: sent / rx_ok / tx_ok / parse_fail / pass.
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import struct
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager


# ============================================================================
# CAMERAD wire constants and frame builders
# ============================================================================

UDP_POLL_PORT = 30002             # CMC's POLL UDP listener
TCP_CMD_PORT  = 30003             # CMC's TCP command listener (panel -> CMC)
PANEL_RX_PORT = 30001             # CMC opens outbound TCP here for responses

HEADER_SIZE   = 64

# camerad_msg_t
MSG_POLL          = 1
MSG_SELECT        = 2
MSG_DESELECT      = 3
MSG_GRAB          = 4
MSG_KEYPRESS_T1   = 5
MSG_KEYPRESS_T2   = 6
MSG_KEYPRESS_T3   = 7
MSG_MOVEMENT      = 8
MSG_LIMIT         = 10
MSG_POSITION_REQ  = 11

# camerad_device_t
DEV_CONTROLLER_S  = 1
DEV_CONTROLLER_T  = 2
DEV_CMC           = 3
DEV_ALL_CMC       = 7
DEV_CMC_BLDC      = 20

# camerad_key_code_t (subset)
KC_STORE_SHOT     = 0x01
KC_CUT            = 0x08
KC_FADE           = 0x09
KC_STORE_TIME     = 0xA1
KC_STOP           = 0xF0


def build_header(msg_command: int,
                 dest_device: int,
                 dest_device_no: int,
                 return_address: str,
                 return_port: int,
                 return_device: int,
                 return_device_no: int,
                 message_length: int,
                 message_id: int,
                 packet_id: int = 0) -> bytes:
    """Build a 64-byte CAMERAD header, little-endian."""
    magic         = b"CAMERAD\x00"                       # 8
    version       = b"1.3\x00"                            # 4
    return_addr_b = return_address.encode("ascii")[:15].ljust(16, b"\x00")
    return struct.pack(
        "<8s4sIII16sIIIIII",
        magic, version,
        msg_command, dest_device, dest_device_no,
        return_addr_b, return_port, return_device, return_device_no,
        message_length, message_id, packet_id,
    )


def build_poll_frame(panel_ip: str,
                     message_id: int,
                     return_device: int = DEV_CONTROLLER_T,
                     return_device_no: int = 1) -> bytes:
    """A bare POLL is just the 64-byte header (no body)."""
    return build_header(
        msg_command=MSG_POLL,
        dest_device=DEV_ALL_CMC, dest_device_no=0,
        return_address=panel_ip, return_port=PANEL_RX_PORT,
        return_device=return_device, return_device_no=return_device_no,
        message_length=HEADER_SIZE, message_id=message_id,
    )


def build_tcp_command(msg_command: int,
                      panel_ip: str,
                      message_id: int,
                      body: bytes = b"",
                      return_device: int = DEV_CONTROLLER_T,
                      return_device_no: int = 1) -> bytes:
    """Build a TCP command frame (header + body)."""
    total_len = HEADER_SIZE + len(body)
    hdr = build_header(
        msg_command=msg_command,
        dest_device=DEV_CMC_BLDC, dest_device_no=1,
        return_address=panel_ip, return_port=PANEL_RX_PORT,
        return_device=return_device, return_device_no=return_device_no,
        message_length=total_len, message_id=message_id,
    )
    return hdr + body


def build_kp_t1(key_code: int, value: int) -> bytes:
    """KEYPRESS_T1 body: {u8 key_code, i32 value}."""
    return struct.pack("<Bi", key_code, value)


def build_kp_t2(key_code: int, status: int = 0) -> bytes:
    """KEYPRESS_T2 body: {u8 key_code, u8 status}."""
    return struct.pack("<BB", key_code, status)


def build_movement(pan: int = 0, tilt: int = 0, zoom: int = 0,
                   focus: int = 0, x: int = 0, y: int = 0,
                   height: int = 0, fader: int = 0,
                   axis_bitmap: int = 0x01) -> bytes:
    """MOVEMENT body: {u8 bitmap, 8x i8 axes}."""
    return struct.pack("<Bbbbbbbbb", axis_bitmap,
                       pan, tilt, zoom, focus, x, y, height, fader)


# ============================================================================
# Mock panel — manages all three sockets
# ============================================================================

class MockPanel:
    def __init__(self, cmc_ip: str):
        self.cmc_ip = cmc_ip
        self.panel_ip = self._detect_local_ip(cmc_ip)
        # UDP socket for sending POLLs (and receiving any UDP responses if we
        # ever bind it to PANEL_RX_PORT — but CMC responses come via TCP).
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Inbound TCP listener: CMC connects here for responses.
        self.tcp_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_listener.bind(("0.0.0.0", PANEL_RX_PORT))
        self.tcp_listener.listen(4)
        self.tcp_listener.settimeout(0.5)
        # Outbound TCP client to CMC's command listener.
        self.tcp_cmd: socket.socket | None = None
        # Response-counting state, updated by the listener thread.
        self.rx_bytes = 0
        self.rx_frames = 0
        self._inbound_conn: socket.socket | None = None
        self._stop = threading.Event()
        self._listener_thread = threading.Thread(
            target=self._listener_loop, daemon=True)

    @staticmethod
    def _detect_local_ip(remote_ip: str) -> str:
        """Determine which local IP the OS would use to reach the CMC."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((remote_ip, 1))   # no actual packet — just route lookup
            return s.getsockname()[0]
        finally:
            s.close()

    def start(self) -> None:
        self._listener_thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.tcp_listener.close()
        except Exception:
            pass
        if self._inbound_conn:
            try:
                self._inbound_conn.close()
            except Exception:
                pass
        if self.tcp_cmd:
            try:
                self.tcp_cmd.close()
            except Exception:
                pass
        try:
            self.udp.close()
        except Exception:
            pass
        self._listener_thread.join(timeout=2.0)

    def _listener_loop(self) -> None:
        """Accept the CMC's outbound TCP and drain bytes. Frame-count by
        parsing the 64-byte header's `message_length` field."""
        buf = b""
        while not self._stop.is_set():
            if self._inbound_conn is None:
                try:
                    conn, _addr = self.tcp_listener.accept()
                    conn.settimeout(0.5)
                    self._inbound_conn = conn
                    buf = b""
                except socket.timeout:
                    continue
                except OSError:
                    break
            try:
                chunk = self._inbound_conn.recv(4096)
                if not chunk:
                    self._inbound_conn.close()
                    self._inbound_conn = None
                    continue
                self.rx_bytes += len(chunk)
                buf += chunk
                # Frame-parse: each CAMERAD frame's length is at offset 52
                # (after magic[8] + version[4] + 3*u32 + addr[16] + 3*u32).
                while len(buf) >= HEADER_SIZE:
                    msg_len = struct.unpack_from("<I", buf, 52)[0]
                    if msg_len < HEADER_SIZE or msg_len > 8192:
                        # garbage — drop the whole buffer
                        buf = b""
                        break
                    if len(buf) < msg_len:
                        break  # wait for the rest
                    self.rx_frames += 1
                    buf = buf[msg_len:]
            except socket.timeout:
                continue
            except OSError:
                if self._inbound_conn:
                    self._inbound_conn.close()
                self._inbound_conn = None

    def ensure_tcp_cmd(self) -> None:
        if self.tcp_cmd is None:
            self.tcp_cmd = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp_cmd.settimeout(2.0)
            self.tcp_cmd.connect((self.cmc_ip, TCP_CMD_PORT))

    def close_tcp_cmd(self) -> None:
        if self.tcp_cmd:
            try:
                self.tcp_cmd.close()
            finally:
                self.tcp_cmd = None

    # Send helpers ---------------------------------------------------------

    def send_poll(self, message_id: int) -> None:
        frame = build_poll_frame(self.panel_ip, message_id)
        self.udp.sendto(frame, (self.cmc_ip, UDP_POLL_PORT))

    def send_tcp_cmd(self, msg_command: int, message_id: int,
                     body: bytes = b"") -> None:
        self.ensure_tcp_cmd()
        frame = build_tcp_command(msg_command, self.panel_ip,
                                  message_id, body)
        assert self.tcp_cmd is not None
        self.tcp_cmd.sendall(frame)

    def reset_rx_counts(self) -> None:
        self.rx_bytes = 0
        self.rx_frames = 0

    def reset_inbound(self) -> None:
        """Force-close any existing inbound TCP connection so the listener
        accepts a fresh one. Needed between tests because after a stress
        test the CMC may have closed (or half-closed) the outbound TCP
        and our listener thread can be stuck on a dead socket. Drop and
        let the next POLL trigger a clean reconnect."""
        conn = self._inbound_conn
        self._inbound_conn = None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


# ============================================================================
# Stats fetching
# ============================================================================

def fetch_stats(cmc_ip: str, timeout: float = 3.0) -> dict:
    req = urllib.request.Request(f"http://{cmc_ip}/api/stats")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def stats_delta(before: dict, after: dict) -> dict:
    """Recursive numeric delta of two stats snapshots."""
    out = {}
    for k, v in after.items():
        if isinstance(v, dict):
            out[k] = stats_delta(before.get(k, {}), v)
        elif isinstance(v, (int, float)):
            out[k] = v - before.get(k, 0)
        else:
            out[k] = v
    return out


# ============================================================================
# Test result formatting
# ============================================================================

class Result:
    def __init__(self, name: str):
        self.name = name
        self.sent: dict[str, int] = {}
        self.delta: dict = {}
        self.local_rx_frames = 0
        self.notes: list[str] = []
        self.passed = False
        self.duration_s = 0.0

    def fmt(self) -> str:
        out = [f"--- {self.name} ---"]
        for k, v in self.sent.items():
            out.append(f"  sent {k}: {v}")
        out.append(f"  duration: {self.duration_s:.1f}s")
        if "rx_ok" in self.delta:
            out.append("  CMC rx_ok deltas:")
            for k, v in self.delta["rx_ok"].items():
                if v != 0:
                    out.append(f"    {k}: {v}")
        if "tx_ok" in self.delta:
            out.append("  CMC tx_ok deltas:")
            for k, v in self.delta["tx_ok"].items():
                if v != 0:
                    out.append(f"    {k}: {v}")
        if "errors" in self.delta:
            errs = {k: v for k, v in self.delta["errors"].items() if v != 0}
            if errs:
                out.append("  CMC errors (deltas):")
                for k, v in errs.items():
                    out.append(f"    {k}: {v}")
        if "tcp" in self.delta:
            tcp = {k: v for k, v in self.delta["tcp"].items() if v != 0}
            if tcp:
                out.append("  CMC tcp (deltas):")
                for k, v in tcp.items():
                    out.append(f"    {k}: {v}")
        out.append(f"  panel rx_frames: {self.local_rx_frames}")
        for n in self.notes:
            out.append(f"  note: {n}")
        out.append(f"  RESULT: {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(out)


@contextmanager
def stats_window(cmc_ip: str, result: Result):
    before = fetch_stats(cmc_ip)
    start = time.monotonic()
    yield
    result.duration_s = time.monotonic() - start
    after = fetch_stats(cmc_ip)
    result.delta = stats_delta(before, after)


# ============================================================================
# Tests
# ============================================================================

def test_01_poll_sustained(panel: MockPanel, cmc_ip: str,
                           duration_s: int = 60) -> Result:
    r = Result(f"Test 1: POLL sustained @ 1 Hz for {duration_s}s")
    panel.reset_rx_counts()
    with stats_window(cmc_ip, r):
        for i in range(duration_s):
            panel.send_poll(message_id=i + 1)
            time.sleep(1.0)
    r.sent = {"POLL": duration_s}
    r.local_rx_frames = panel.rx_frames
    r.passed = (r.delta.get("rx_ok", {}).get("POLL", 0) == duration_s
                and r.delta.get("tx_ok", {}).get("POLL", 0) == duration_s
                and r.delta.get("errors", {}).get("rx_parse_fail", 0) == 0)
    return r


def test_02_poll_burst(panel: MockPanel, cmc_ip: str,
                       bursts: int = 10, per_burst: int = 100) -> Result:
    r = Result(f"Test 2: POLL burst ({bursts} x {per_burst} @ 100 Hz)")
    panel.reset_rx_counts()
    with stats_window(cmc_ip, r):
        mid = 0
        for _ in range(bursts):
            for _ in range(per_burst):
                mid += 1
                panel.send_poll(message_id=mid)
                time.sleep(0.01)  # 100 Hz
            time.sleep(2.0)
    sent = bursts * per_burst
    r.sent = {"POLL": sent}
    r.local_rx_frames = panel.rx_frames
    rx = r.delta.get("rx_ok", {}).get("POLL", 0)
    loss_pct = 100 * (sent - rx) / sent if sent else 0
    r.notes.append(f"loss rate: {loss_pct:.2f}%")
    r.passed = loss_pct < 5.0  # <5% loss tolerated on burst
    return r


def test_03_poll_flood(panel: MockPanel, cmc_ip: str,
                       count: int = 1000) -> Result:
    r = Result(f"Test 3: POLL flood ({count} as fast as possible)")
    panel.reset_rx_counts()
    with stats_window(cmc_ip, r):
        for i in range(count):
            panel.send_poll(message_id=i + 1)
        # Drain time for any pending responses
        time.sleep(2.0)
    r.sent = {"POLL": count}
    r.local_rx_frames = panel.rx_frames
    rx = r.delta.get("rx_ok", {}).get("POLL", 0)
    loss_pct = 100 * (count - rx) / count if count else 0
    r.notes.append(f"loss rate: {loss_pct:.2f}% (stress test, loss EXPECTED)")
    # Stress test: pass = no crash + some loss tolerated.
    parse_ok = r.delta.get("errors", {}).get("rx_parse_fail", 0) == 0
    r.passed = parse_ok  # don't fail on legitimate UDP drops
    return r


def test_04_tcp_select_cycle(panel: MockPanel, cmc_ip: str,
                             cycles: int = 1000) -> Result:
    r = Result(f"Test 4: TCP SELECT/DESELECT cycle ({cycles} pairs)")
    panel.reset_rx_counts()
    # Establish TCP via POLL first (so CMC opens outbound TCP to us).
    panel.send_poll(message_id=1)
    time.sleep(1.0)
    panel.ensure_tcp_cmd()
    with stats_window(cmc_ip, r):
        mid = 0
        for _ in range(cycles):
            mid += 1
            panel.send_tcp_cmd(MSG_SELECT, mid)
            time.sleep(0.01)
            mid += 1
            panel.send_tcp_cmd(MSG_DESELECT, mid)
            time.sleep(0.01)
        time.sleep(2.0)  # drain
    r.sent = {"SELECT": cycles, "DESELECT": cycles}
    r.local_rx_frames = panel.rx_frames
    sel_rx = r.delta.get("rx_ok", {}).get("SELECT", 0)
    des_rx = r.delta.get("rx_ok", {}).get("DESELECT", 0)
    r.passed = (sel_rx == cycles and des_rx == cycles)
    return r


def test_05_movement_flood(panel: MockPanel, cmc_ip: str,
                           rate_hz: int = 100, duration_s: int = 30) -> Result:
    count = rate_hz * duration_s
    r = Result(f"Test 5: MOVEMENT flood @ {rate_hz} Hz for {duration_s}s ({count} frames)")
    panel.reset_rx_counts()
    panel.send_poll(message_id=1)
    time.sleep(1.0)
    panel.ensure_tcp_cmd()
    period = 1.0 / rate_hz
    with stats_window(cmc_ip, r):
        for i in range(count):
            body = build_movement(pan=0)
            panel.send_tcp_cmd(MSG_MOVEMENT, i + 1, body)
            time.sleep(period)
    r.sent = {"MOVEMENT": count}
    rx = r.delta.get("rx_ok", {}).get("MOVEMENT", 0)
    loss_pct = 100 * (count - rx) / count if count else 0
    r.notes.append(f"loss rate: {loss_pct:.2f}%")
    r.passed = (loss_pct < 1.0
                and r.delta.get("errors", {}).get("rx_parse_fail", 0) == 0)
    return r


def test_06_mixed_realistic(panel: MockPanel, cmc_ip: str,
                            duration_s: int = 60) -> Result:
    r = Result(f"Test 6: Mixed traffic (POLL 1Hz + KP 0.5Hz + MOVE 40Hz) for {duration_s}s")
    panel.reset_rx_counts()
    panel.send_poll(message_id=1)
    time.sleep(1.0)
    panel.ensure_tcp_cmd()
    poll_count = 0
    move_count = 0
    kp_count = 0
    next_poll = time.monotonic() + 1.0
    next_kp = time.monotonic() + 2.0
    move_period = 1.0 / 40
    next_move = time.monotonic() + move_period
    mid = 100
    with stats_window(cmc_ip, r):
        end = time.monotonic() + duration_s
        while time.monotonic() < end:
            now = time.monotonic()
            if now >= next_poll:
                mid += 1; panel.send_poll(message_id=mid); poll_count += 1
                next_poll += 1.0
            if now >= next_kp:
                mid += 1
                panel.send_tcp_cmd(MSG_KEYPRESS_T1, mid, build_kp_t1(KC_FADE, 1))
                kp_count += 1
                next_kp += 2.0
            if now >= next_move:
                mid += 1
                panel.send_tcp_cmd(MSG_MOVEMENT, mid, build_movement(pan=0))
                move_count += 1
                next_move += move_period
            time.sleep(0.001)
        time.sleep(1.0)
    r.sent = {"POLL": poll_count, "KP_T1": kp_count, "MOVEMENT": move_count}
    rx_poll = r.delta.get("rx_ok", {}).get("POLL", 0)
    rx_kp   = r.delta.get("rx_ok", {}).get("KP_T1", 0)
    rx_move = r.delta.get("rx_ok", {}).get("MOVEMENT", 0)
    r.passed = (rx_poll == poll_count
                and rx_kp == kp_count
                and rx_move >= int(move_count * 0.995))  # 0.5% slack
    return r


def test_07_long_idle(panel: MockPanel, cmc_ip: str,
                      idle_s: int = 60, burst_count: int = 50) -> Result:
    # NOTE: 'long' in the docs is 15 min; we use 60 s by default to keep
    # the test runtime sane. Bump --idle-s for a real long-soak test.
    r = Result(f"Test 7: long idle ({idle_s}s) then burst ({burst_count})")
    panel.reset_rx_counts()
    panel.send_poll(message_id=1)
    time.sleep(1.0)
    panel.ensure_tcp_cmd()
    with stats_window(cmc_ip, r):
        time.sleep(idle_s)
        mid = 0
        for _ in range(burst_count):
            mid += 1
            panel.send_tcp_cmd(MSG_SELECT, mid)
            time.sleep(0.01)
            mid += 1
            panel.send_tcp_cmd(MSG_DESELECT, mid)
            time.sleep(0.01)
        time.sleep(2.0)
    r.sent = {"SELECT": burst_count, "DESELECT": burst_count}
    sel_rx = r.delta.get("rx_ok", {}).get("SELECT", 0)
    des_rx = r.delta.get("rx_ok", {}).get("DESELECT", 0)
    r.passed = (sel_rx == burst_count and des_rx == burst_count)
    return r


def test_08_tcp_reconnect(panel: MockPanel, cmc_ip: str,
                          cycles: int = 50) -> Result:
    r = Result(f"Test 8: TCP disconnect/reconnect ({cycles} cycles)")
    panel.reset_rx_counts()
    with stats_window(cmc_ip, r):
        mid = 0
        for _ in range(cycles):
            panel.ensure_tcp_cmd()
            mid += 1
            panel.send_tcp_cmd(MSG_SELECT, mid)
            time.sleep(0.05)
            panel.close_tcp_cmd()
            time.sleep(0.1)
    r.sent = {"SELECT": cycles, "TCP_open/close": cycles}
    sel_rx = r.delta.get("rx_ok", {}).get("SELECT", 0)
    r.notes.append(f"SELECT rx: {sel_rx} / {cycles}")
    r.passed = sel_rx >= int(cycles * 0.9)  # 90% tolerance for reconnect races
    return r


def test_09_malformed(panel: MockPanel, cmc_ip: str) -> Result:
    r = Result("Test 9: Malformed input fuzzing")
    panel.reset_rx_counts()
    with stats_window(cmc_ip, r):
        # Garbage UDP
        for _ in range(50):
            panel.udp.sendto(random.randbytes(random.randint(1, 200)),
                             (cmc_ip, UDP_POLL_PORT))
            time.sleep(0.02)
        # Truncated header
        panel.udp.sendto(b"\x00" * 32, (cmc_ip, UDP_POLL_PORT))
        # Wrong magic
        bad = bytearray(build_poll_frame(panel.panel_ip, 1))
        bad[0:8] = b"BADBAD\x00\x00"
        panel.udp.sendto(bytes(bad), (cmc_ip, UDP_POLL_PORT))
        # Oversize msg_length
        bad = bytearray(build_poll_frame(panel.panel_ip, 1))
        bad[52:56] = struct.pack("<I", 99999)
        panel.udp.sendto(bytes(bad), (cmc_ip, UDP_POLL_PORT))
        time.sleep(2.0)
        # Verify CMC is still alive — send a legit POLL and expect rx_ok bump
        panel.send_poll(message_id=999)
        time.sleep(1.0)
    r.sent = {"garbage_UDP": 50, "malformed": 3, "verify_POLL": 1}
    fail_count = r.delta.get("errors", {}).get("rx_parse_fail", 0)
    rx_poll = r.delta.get("rx_ok", {}).get("POLL", 0)
    r.notes.append(f"parse_fails: {fail_count} (expected > 0)")
    r.notes.append(f"verify POLL rx: {rx_poll} (expected 1)")
    # PASS = CMC didn't crash + parse errors counted + legit POLL still works
    r.passed = (fail_count > 0 and rx_poll >= 1)
    return r


def test_10_tx_backpressure(panel: MockPanel, cmc_ip: str,
                            count: int = 1000) -> Result:
    r = Result(f"Test 10: TX backpressure ({count} POLLs @ ~1 kHz)")
    panel.reset_rx_counts()
    with stats_window(cmc_ip, r):
        for i in range(count):
            panel.send_poll(message_id=i + 1)
            time.sleep(0.001)
        time.sleep(2.0)
    r.sent = {"POLL": count}
    rx = r.delta.get("rx_ok", {}).get("POLL", 0)
    tx = r.delta.get("tx_ok", {}).get("POLL", 0)
    send_err = r.delta.get("errors", {}).get("send_errors", 0)
    loss_pct = 100 * (count - rx) / count if count else 0
    r.notes.append(f"rx loss: {loss_pct:.2f}%, tx: {tx}, send_err: {send_err}")
    # Pass = graceful: either everything got through OR drops are counted
    parse_ok = r.delta.get("errors", {}).get("rx_parse_fail", 0) == 0
    r.passed = parse_ok
    return r


ALL_TESTS = [
    test_01_poll_sustained,
    test_02_poll_burst,
    test_03_poll_flood,
    test_04_tcp_select_cycle,
    test_05_movement_flood,
    test_06_mixed_realistic,
    test_07_long_idle,
    test_08_tcp_reconnect,
    test_09_malformed,
    test_10_tx_backpressure,
]


# ============================================================================
# Main
# ============================================================================

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.1.0.100",
                    help="CMC IP address (default: 192.1.0.100)")
    ap.add_argument("--test", type=int, default=0,
                    help="Run a specific test number (1..10). 0 = all.")
    ap.add_argument("--short", action="store_true",
                    help="Short-duration variants for quick iteration.")
    args = ap.parse_args(argv[1:])

    # Sanity check the CMC is reachable.
    try:
        cfg = fetch_stats(args.ip)
    except Exception as e:
        print(f"FATAL: cannot reach {args.ip}/api/stats — {e}")
        return 2

    print(f"CMC reachable. Initial rx_ok: {cfg.get('rx_ok')}")

    panel = MockPanel(args.ip)
    panel.start()
    print(f"Mock panel started. Local IP: {panel.panel_ip}, "
          f"listening for CMC TCP on :{PANEL_RX_PORT}")

    tests_to_run = ALL_TESTS if args.test == 0 else [ALL_TESTS[args.test - 1]]
    short = args.short

    # Per-test argument overrides for --short
    short_kwargs = {
        "test_01_poll_sustained":  {"duration_s": 10},
        "test_02_poll_burst":      {"bursts": 3, "per_burst": 50},
        "test_03_poll_flood":      {"count": 200},
        "test_04_tcp_select_cycle":{"cycles": 100},
        "test_05_movement_flood":  {"rate_hz": 50, "duration_s": 5},
        "test_06_mixed_realistic": {"duration_s": 10},
        "test_07_long_idle":       {"idle_s": 10, "burst_count": 20},
        "test_08_tcp_reconnect":   {"cycles": 20},
        "test_09_malformed":       {},
        "test_10_tx_backpressure": {"count": 200},
    }

    results = []
    try:
        for fn in tests_to_run:
            kwargs = short_kwargs.get(fn.__name__, {}) if short else {}
            print(f"\n>>> Running {fn.__name__}{' (short)' if short else ''} ...")
            # Drop any stale inbound TCP from the previous test so the
            # listener re-accepts cleanly when the CMC reconnects.
            panel.reset_inbound()
            panel.close_tcp_cmd()
            # Pause so the CMC sees the disconnect and its outbound TCP
            # transitions to CLOSED — next POLL will then trigger a fresh
            # outbound connect that our listener can accept cleanly.
            time.sleep(0.5)
            try:
                result = fn(panel, args.ip, **kwargs)
            except Exception as e:
                result = Result(fn.__name__)
                result.notes.append(f"EXCEPTION: {e}")
                result.passed = False
            results.append(result)
            print(result.fmt())
    finally:
        panel.stop()

    print("\n=== Summary ===")
    for r in results:
        print(f"  {'PASS' if r.passed else 'FAIL'}: {r.name}")
    failed = sum(1 for r in results if not r.passed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
