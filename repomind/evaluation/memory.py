"""How much memory the machine has left, so that a long run can stop before it starves other programs."""

from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable

# A run that loads the local model starts only with this much free physical memory and commit space.
# That is the model container's 8 GB limit, 1 GB for Python and 3 GB of margin.
START_MINIMUM_GB = 12.0


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


@dataclass(frozen=True)
class MemoryReading:
    """Free physical memory and free commit space, in GB. None where it cannot be read."""

    physical_gb: float | None
    commit_gb: float | None


def read_memory() -> MemoryReading:
    if sys.platform == "win32":
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return MemoryReading(None, None)
        return MemoryReading(status.ullAvailPhys / 1024**3, status.ullAvailPageFile / 1024**3)
    try:
        physical = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024**3
    except (AttributeError, ValueError, OSError):
        physical = None
    # Commit space is a Windows measure. Elsewhere it is unknown, which a start check refuses.
    return MemoryReading(physical, None)


def available_memory_gb() -> float | None:
    """The smaller of free physical memory and free commit space, or None where it cannot be read.

    Commit space counts because Windows can run out of it while physical memory is still free.
    Elsewhere this is free physical memory, which is what a check between questions needs.
    """
    reading = read_memory()
    known = [value for value in (reading.physical_gb, reading.commit_gb) if value is not None]
    return min(known) if known else None


def _gb(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.2f} GB"


@dataclass(frozen=True)
class StartCheck:
    minimum_gb: float
    readings: tuple[MemoryReading, ...]

    @property
    def refusal(self) -> str | None:
        """Why the model must not be loaded, or None when every reading clears the minimum."""
        for reading in self.readings:
            for name, value in (("physical memory", reading.physical_gb), ("commit space", reading.commit_gb)):
                if value is None:
                    return f"free {name} cannot be read"
                if value < self.minimum_gb:
                    return f"free {name} is {value:.2f} GB, below {self.minimum_gb:.0f} GB"
        return None

    def describe(self) -> str:
        return "; ".join(f"physical {_gb(r.physical_gb)}, commit {_gb(r.commit_gb)}" for r in self.readings)


def check_before_loading(
    minimum_gb: float = START_MINIMUM_GB,
    *,
    read: Callable[[], MemoryReading] | None = None,
    readings: int = 2,
    pause_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> StartCheck:
    """Reads memory a few times, a moment apart, before anything loads the model.

    One reading can catch a moment when another program has just let go of memory, so each one has
    to clear the minimum, for physical memory and commit space alike. An unreadable value refuses.
    """
    read = read or read_memory
    taken = []
    for index in range(readings):
        if index:
            sleep(pause_seconds)
        taken.append(read())
    return StartCheck(minimum_gb, tuple(taken))
