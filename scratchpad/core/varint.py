"""LEB128 varints (unsigned and zigzag-signed).

Encoding: little-endian base-128, continuation bit 0x80 on every byte but the
last.  Decoding is strict:

* at most :data:`MAX_VARINT_BYTES` bytes (64 bit values),
* no overlong encodings (a trailing 0x00 continuation byte is rejected),
* truncation raises :class:`VarintError`.

Strictness matters: the event log relies on a bit-exact canonical encoding so
that a record's CRC is reproducible, and a rejected varint is one more way to
notice a corrupt record.
"""

from __future__ import annotations

MAX_VARINT_BYTES = 10
MAX_VALUE = (1 << 64) - 1


class VarintError(ValueError):
    """Raised for truncated, overlong or out-of-range varints."""


def varint_size(value: int) -> int:
    """Number of bytes :func:`encode_uvarint` would produce for ``value``."""
    if value < 0:
        raise VarintError(f"negative value {value}")
    size = 1
    while value >= 0x80:
        value >>= 7
        size += 1
    return size


def append_uvarint(out: bytearray, value: int) -> None:
    """Append the unsigned LEB128 encoding of ``value`` to ``out``."""
    if value < 0:
        raise VarintError(f"negative value {value}")
    if value > MAX_VALUE:
        raise VarintError(f"value {value} exceeds 64 bits")
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def encode_uvarint(value: int) -> bytes:
    """Unsigned LEB128 encoding of ``value``."""
    out = bytearray()
    append_uvarint(out, value)
    return bytes(out)


def decode_uvarint(buf: bytes | bytearray | memoryview, offset: int = 0) -> tuple[int, int]:
    """Decode an unsigned LEB128 varint.

    Returns ``(value, next_offset)``.  Raises :class:`VarintError` if the buffer
    ends inside the varint or the encoding is not canonical.
    """
    result = 0
    shift = 0
    index = offset
    size = len(buf)
    while True:
        if index >= size:
            raise VarintError(f"truncated varint at offset {offset}")
        if index - offset >= MAX_VARINT_BYTES:
            raise VarintError(f"varint at offset {offset} is longer than {MAX_VARINT_BYTES} bytes")
        byte = buf[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            if byte == 0 and index - offset > 1:
                raise VarintError(f"overlong varint at offset {offset}")
            if result > MAX_VALUE:
                raise VarintError(f"varint at offset {offset} exceeds 64 bits")
            return result, index
        shift += 7


def zigzag_encode(value: int) -> int:
    """Map a signed integer onto an unsigned one (small magnitudes stay small)."""
    return (value << 1) ^ (value >> 63) if value < 0 else value << 1


def zigzag_decode(value: int) -> int:
    """Inverse of :func:`zigzag_encode`."""
    return (value >> 1) ^ -(value & 1)


def append_svarint(out: bytearray, value: int) -> None:
    """Append the zigzag LEB128 encoding of ``value`` to ``out``."""
    append_uvarint(out, zigzag_encode(value))


def encode_svarint(value: int) -> bytes:
    """Zigzag LEB128 encoding of ``value``."""
    return encode_uvarint(zigzag_encode(value))


def decode_svarint(buf: bytes | bytearray | memoryview, offset: int = 0) -> tuple[int, int]:
    """Decode a zigzag LEB128 varint.  Returns ``(value, next_offset)``."""
    raw, index = decode_uvarint(buf, offset)
    return zigzag_decode(raw), index
