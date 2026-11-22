# SPDX-License-Identifier: Apache-2.0
"""Content-defined chunks for large files (ADR-054 §2.1: 64 KB to 4 MB).

A gear rolling hash (as in FastCDC) over the bytes after a minimum length
chooses cut points, so an insertion near the start of a file moves one or
two chunk boundaries instead of every one. Small files are not chunked;
appdata packs them together instead.

The gear table is keyed per zone from the account's naming key, so chunk
boundaries of a known file differ between accounts and zones, and the
engine pads every encrypted piece to a multiple of 64 KiB, so lengths say
little (ePrint 2025/558 on fingerprinting through chunk sizes).
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from typing import BinaryIO

MIN = 64 * 1024
AVERAGE = 1024 * 1024
MAX = 4 * 1024 * 1024
MASK = (1 << 20) - 1        # one cut in about 1 MiB after the minimum
_MOD = (1 << 64) - 1
_TABLES: dict[bytes, list[int]] = {}


def gear(key: bytes) -> list[int]:
    table = _TABLES.get(key)
    if table is None:
        table = [int.from_bytes(hmac.new(key, b"luma-sync-gear-v2" + bytes([i]), hashlib.sha256).digest()[:8], "big") for i in range(256)]
        if len(_TABLES) > 64:
            _TABLES.clear()
        _TABLES[key] = table
    return table


def cut(data: bytes | memoryview, key: bytes, start: int = 0) -> int:
    """Length of the first chunk of data[start:]."""
    end = len(data)
    if end - start <= MIN:
        return end - start
    limit = min(end, start + MAX)
    value = 0
    table = gear(key)
    index = start + MIN
    while index < limit:
        value = ((value << 1) + table[data[index]]) & _MOD
        if not value & MASK:
            return index - start + 1
        index += 1
    return limit - start


def chunks(stream: BinaryIO, key: bytes) -> Iterator[bytes]:
    buffer = b""
    while True:
        more = stream.read(MAX * 2)
        buffer += more
        while len(buffer) >= MAX or (not more and buffer):
            length = cut(buffer, key)
            yield buffer[:length]
            buffer = buffer[length:]
        if not more:
            return
