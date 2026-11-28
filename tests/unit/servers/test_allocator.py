"""The gateway fixes glibc's mmap threshold, so a large freed buffer leaves the process."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from serving.servers import allocator, app as app_module

BACKEND = Path(allocator.__file__).resolve().parents[2]


def _glibc() -> bool:
    try:
        return (os.confstr("CS_GNU_LIBC_VERSION") or "").startswith("glibc")
    except (AttributeError, OSError, ValueError):
        return False


glibc_only = pytest.mark.skipif(
    not _glibc() or not hasattr(ctypes.CDLL(None), "mallinfo2"),
    reason="the mmap threshold is glibc's; mallinfo2 needs glibc 2.33",
)

#: Frees an 8 MiB block, then prints how much of a 4 MiB one glibc mapped on
#: its own (KiB), after pinning the threshold when given "pin".
_PROBE = """
import ctypes, sys
from serving.servers import allocator

class Mallinfo2(ctypes.Structure):
    _fields_ = [(name, ctypes.c_size_t) for name in (
        "arena", "ordblks", "smblks", "hblks", "hblkhd",
        "usmblks", "fsmblks", "uordblks", "fordblks", "keepcost")]

libc = ctypes.CDLL(None)
libc.malloc.argtypes, libc.malloc.restype = (ctypes.c_size_t,), ctypes.c_void_p
libc.free.argtypes, libc.free.restype = (ctypes.c_void_p,), None
libc.mallinfo2.restype = Mallinfo2
if sys.argv[1:] == ["pin"]:
    assert allocator.pin_mmap_threshold()
libc.free(libc.malloc(8 << 20))
mapped = libc.mallinfo2().hblkhd
block = libc.malloc(4 << 20)
print((libc.mallinfo2().hblkhd - mapped) // 1024)
libc.free(block)
"""


def _mapped_kib(*args: str) -> int:
    """Run :data:`_PROBE` in a fresh interpreter.

    glibc maps a large block only when the heap's free space cannot already
    hold it, and the heap of a process that has run other tests may by now
    have room; a new one does not.
    """
    env = {
        k: v for k, v in os.environ.items() if k not in ("MALLOC_MMAP_THRESHOLD_", "GLIBC_TUNABLES")
    }
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, *args],
        env={**env, "PYTHONPATH": str(BACKEND)},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return int(result.stdout)


@pytest.fixture(autouse=True)
def _no_operator_setting(monkeypatch):
    monkeypatch.delenv("MALLOC_MMAP_THRESHOLD_", raising=False)
    monkeypatch.delenv("GLIBC_TUNABLES", raising=False)


def _unexpected(*args, **kwargs):
    raise AssertionError("the C library must not be asked")


@glibc_only
def test_unpinned_glibc_grows_the_heap_for_a_large_block_once_a_larger_one_was_freed():
    """The behaviour the pin turns off: freeing 8 MiB raised the threshold past 4 MiB."""
    assert _mapped_kib() == 0


@glibc_only
def test_pinned_a_large_block_keeps_its_own_mapping():
    assert _mapped_kib("pin") >= 4 << 10


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("MALLOC_MMAP_THRESHOLD_", "65536"),
        ("GLIBC_TUNABLES", "glibc.malloc.arena_max=2:glibc.malloc.mmap_threshold=65536"),
    ],
)
def test_an_operator_setting_stands(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(allocator.ctypes, "CDLL", _unexpected)

    assert allocator.pin_mmap_threshold() is False


@glibc_only
def test_another_tunable_does_not_stop_the_pin(monkeypatch):
    monkeypatch.setenv("GLIBC_TUNABLES", "glibc.malloc.arena_max=2")

    assert allocator.pin_mmap_threshold() is True


@pytest.mark.parametrize("answer", [None, ValueError("unrecognized configuration name")])
def test_another_c_library_is_left_alone(monkeypatch, answer):
    def confstr(name):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(allocator.os, "confstr", confstr)
    monkeypatch.setattr(allocator.ctypes, "CDLL", _unexpected)

    assert allocator.pin_mmap_threshold() is False


async def test_the_gateway_pins_the_threshold_before_it_starts(monkeypatch):
    order: list[str] = []

    async def initialize():
        order.append("initialize")
        return object()

    monkeypatch.setattr(allocator, "pin_mmap_threshold", lambda: order.append("pin"))
    monkeypatch.setattr(app_module.bootstrap, "initialize", initialize)
    monkeypatch.setattr(app_module.bootstrap, "shutdown", AsyncMock())

    async with app_module.lifespan(FastAPI()):
        pass

    assert order == ["pin", "initialize"]
