"""Rolling telemetry buffer shared by the graph windows.

The network client emits samples on the GUI thread; the main window feeds them
here, and each graph window pulls ordered arrays on its own redraw timer. This
decouples the (up to 1 kHz) sample rate from the (~30 Hz) plot redraw rate.
"""
from __future__ import annotations

import numpy as np

from .client import TelemetrySample


class RingBuffer:
    """Fixed-capacity ring of (time, value) pairs returned in chronological order."""

    def __init__(self, capacity: int):
        self.cap = capacity
        self.t = np.zeros(capacity, dtype=np.float64)
        self.v = np.zeros(capacity, dtype=np.float64)
        self.head = 0
        self.count = 0

    def append(self, t: float, v: float) -> None:
        self.t[self.head] = t
        self.v[self.head] = v
        self.head = (self.head + 1) % self.cap
        if self.count < self.cap:
            self.count += 1

    def data(self) -> tuple[np.ndarray, np.ndarray]:
        if self.count < self.cap:
            return self.t[:self.count], self.v[:self.count]
        return (np.concatenate((self.t[self.head:], self.t[:self.head])),
                np.concatenate((self.v[self.head:], self.v[:self.head])))

    def clear(self) -> None:
        self.head = 0
        self.count = 0


class TelemetryBuffer:
    """Per-channel rolling buffers, keyed by channel name."""

    def __init__(self, capacity: int = 120_000, rate_hz: float = 1000.0):
        self.capacity = capacity
        self.rate_hz = rate_hz
        self.channels: dict[str, RingBuffer] = {}
        self.latest_t = 0.0

    def set_rate(self, rate_hz: float) -> None:
        self.rate_hz = max(1.0, rate_hz)

    def _ring(self, name: str) -> RingBuffer:
        rb = self.channels.get(name)
        if rb is None:
            rb = RingBuffer(self.capacity)
            self.channels[name] = rb
        return rb

    def add_samples(self, samples: list[TelemetrySample]) -> None:
        period = 1.0 / self.rate_hz
        for s in samples:
            t = s.counter * period
            self.latest_t = t
            for name, value in s.values.items():
                self._ring(name).append(t, float(value))

    def names(self) -> list[str]:
        return sorted(self.channels.keys())

    def get(self, name: str) -> tuple[np.ndarray, np.ndarray] | None:
        rb = self.channels.get(name)
        return rb.data() if rb else None

    def clear(self) -> None:
        for rb in self.channels.values():
            rb.clear()
        self.latest_t = 0.0
