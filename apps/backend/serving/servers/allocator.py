"""Give the memory of large freed buffers back to the operating system.

When the heap's free space cannot hold a block of at least its *mmap
threshold*, glibc's ``malloc`` gives the block a mapping of its own, which
``free`` unmaps. The threshold starts at 128 KiB, but each time such a block is
freed glibc raises it to that block's size, up to 32 MiB, and from then on
grows the heap for any block below it. The heap shrinks only from its top,
which the small objects allocated in between hold in place. The gateway reads,
parses and forwards request bodies and replies of hundreds of kilobytes to
megabytes, the contexts coding agents send: after the first few, every such
buffer comes from the heap, which grows to the most those buffers ever took at
once and stays there after the load has gone.

Setting the threshold turns that adjustment off. :func:`pin_mmap_threshold`
sets it to glibc's own starting value, so the heap no longer grows for a large
block: the block gets its own mapping again, and its memory leaves the process
when it is freed. After bursts of 100 KB to 1.5 MB prompts the gateway settled
at 185 to 200 MB where it had stayed at 250 to 330 MB, used less on average
during them, and served them as fast. The price is the kernel zeroing the
pages of each new mapping: about 6% more CPU when every request is that large.
Blocks below 128 KiB, the bulk of what the gateway allocates, are served as
before.
"""

from __future__ import annotations

import ctypes
import logging
import os

logger = logging.getLogger(__name__)

#: ``M_MMAP_THRESHOLD`` in glibc's ``<malloc.h>``.
_M_MMAP_THRESHOLD = -3

#: The threshold :func:`pin_mmap_threshold` sets: glibc's starting value.
MMAP_THRESHOLD = 128 * 1024

#: Where an operator sets the threshold for glibc, read when the process starts.
_OPERATOR_ENV = "MALLOC_MMAP_THRESHOLD_"
_TUNABLES_ENV = "GLIBC_TUNABLES"
_TUNABLE = "glibc.malloc.mmap_threshold"


def pin_mmap_threshold() -> bool:
    """Fix glibc's mmap threshold at :data:`MMAP_THRESHOLD`.

    Does nothing under another C library, or when the operator has set the
    threshold through ``MALLOC_MMAP_THRESHOLD_`` or ``GLIBC_TUNABLES``: theirs
    stands.

    Returns:
        Whether the threshold was set.
    """
    if _OPERATOR_ENV in os.environ or _TUNABLE in os.environ.get(_TUNABLES_ENV, ""):
        return False
    try:
        libc = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (AttributeError, OSError, ValueError):
        libc = ""
    if not libc.startswith("glibc"):
        return False
    try:
        mallopt = ctypes.CDLL(None).mallopt
    except (AttributeError, OSError):
        return False
    mallopt.argtypes = (ctypes.c_int, ctypes.c_int)
    mallopt.restype = ctypes.c_int
    if mallopt(_M_MMAP_THRESHOLD, MMAP_THRESHOLD) != 1:
        logger.warning("glibc refused an mmap threshold of %d bytes", MMAP_THRESHOLD)
        return False
    logger.info(
        "Allocator: glibc mmap threshold fixed at %d KiB, so freed large buffers "
        "return to the system",
        MMAP_THRESHOLD // 1024,
    )
    return True
