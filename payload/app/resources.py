from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import psutil

from .config import Settings


@dataclass(frozen=True)
class ResourceSnapshot:
    cpu_percent: float
    memory_available: int
    swap_used: int
    disk_free: int
    network_bytes_per_second: float
    sampled_at: float


class ResourceMonitor:
    def __init__(self, download_dir: Path):
        self.download_dir = download_dir
        self._cpu_samples: deque[float] = deque(maxlen=5)
        self._last_net = psutil.net_io_counters()
        self._last_time = time.monotonic()
        psutil.cpu_percent(interval=None)

    def sample(self) -> ResourceSnapshot:
        now = time.monotonic()
        cpu = psutil.cpu_percent(interval=None)
        self._cpu_samples.append(float(cpu))
        cpu_average = sum(self._cpu_samples) / len(self._cpu_samples)
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage(str(self.download_dir))
        net = psutil.net_io_counters()
        elapsed = max(0.001, now - self._last_time)
        network_delta = (net.bytes_recv - self._last_net.bytes_recv) + (
            net.bytes_sent - self._last_net.bytes_sent
        )
        network_rate = max(0.0, network_delta / elapsed)
        self._last_net = net
        self._last_time = now
        return ResourceSnapshot(
            cpu_percent=cpu_average,
            memory_available=int(memory.available),
            swap_used=int(swap.used),
            disk_free=int(disk.free),
            network_bytes_per_second=network_rate,
            sampled_at=time.time(),
        )


class AdaptiveWindow:
    """Feedback-based task window with no user-defined fixed task count."""

    def __init__(self, settings: Settings, name: str):
        self.settings = settings
        self.name = name
        self.value = 1
        self._last_network_rate = 0.0
        self._high_pressure_cycles = 0

    @staticmethod
    def _memory_derived_ceiling(memory_available: int) -> int:
        # A safety ceiling derived only from current memory, not a fixed task cap.
        # Each active orchestration task receives at least a 32 MiB allowance.
        return max(1, memory_available // (32 * 1024**2))

    def update(
        self,
        snapshot: ResourceSnapshot,
        *,
        recent_errors: int,
        destination_healthy: bool,
        demand_present: bool,
    ) -> int:
        memory_cap = self._memory_derived_ceiling(snapshot.memory_available)
        if (
            snapshot.memory_available < self.settings.memory_hard_min_bytes
            or not destination_healthy
            or (
                self.name == "download"
                and snapshot.disk_free <= self.settings.min_free_disk_bytes
            )
        ):
            self.value = 0
            return self.value

        if snapshot.cpu_percent >= self.settings.cpu_pressure or recent_errors >= 3:
            self._high_pressure_cycles += 1
        else:
            self._high_pressure_cycles = 0

        if self._high_pressure_cycles >= 2:
            self.value = max(
                1, math.floor(max(1, self.value) * self.settings.ramp_down_factor)
            )
        elif (
            (
                snapshot.cpu_percent < self.settings.cpu_pressure
                and snapshot.cpu_percent > self.settings.cpu_target_high
            )
            or snapshot.memory_available < self.settings.memory_soft_min_bytes
        ):
            self.value = max(1, self.value - 1)
        elif (
            demand_present
            and snapshot.cpu_percent < self.settings.cpu_target_low
            and recent_errors == 0
        ):
            throughput_not_worse = (
                self._last_network_rate <= 0
                or snapshot.network_bytes_per_second
                >= self._last_network_rate * 0.90
            )
            if throughput_not_worse:
                self.value += self.settings.ramp_up_step

        self.value = min(max(1, self.value), memory_cap)
        self._last_network_rate = snapshot.network_bytes_per_second
        return self.value
