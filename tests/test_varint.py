"""LEB128 varint encoding."""
from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from scratchpad.core.varint import (
    MAX_VALUE,
    VarintError,
    decode_svarint,
    decode_uvarint,
    encode_svarint,
    encode_uvarint,
    varint_size,
    zigzag_decode,
    zigzag_encode,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (300, b"\xac\x02"),
        (16383, b"\xff\x7f"),
        (16384, b"\x80\x80\x01"),
    ],
)
def test_known_encodings(value: int, expected: bytes) -> None:
    assert encode_uvarint(value) == expected
    assert decode_uvarint(expected) == (value, len(expected))


def test_size_boundaries_match_the_documented_record_cost() -> None:
    # The per-record cost table in eventlog.py depends on these thresholds.
    assert varint_size(127) == 1
    assert varint_size(128) == 2
    assert varint_size(16383) == 2
    assert varint_size(16384) == 3
    assert varint_size(268_435_455) == 4      # 268 ms in nanoseconds
    assert varint_size(268_435_456) == 5
    assert varint_size(34_359_738_367) == 5   # 34 s in nanoseconds


@given(st.integers(min_value=0, max_value=MAX_VALUE))
def test_unsigned_round_trip(value: int) -> None:
    raw = encode_uvarint(value)
    assert len(raw) == varint_size(value)
    assert decode_uvarint(raw) == (value, len(raw))


@given(st.integers(min_value=-(2**62), max_value=2**62))
def test_signed_round_trip(value: int) -> None:
    raw = encode_svarint(value)
    assert decode_svarint(raw) == (value, len(raw))
    assert zigzag_decode(zigzag_encode(value)) == value


def test_zigzag_keeps_small_magnitudes_small() -> None:
    for value in (-1, 0, 1, -63, 63):
        assert len(encode_svarint(value)) == 1


@given(st.lists(st.integers(min_value=0, max_value=MAX_VALUE), min_size=1, max_size=20))
def test_concatenated_values_decode_in_order(values: list[int]) -> None:
    buf = b"".join(encode_uvarint(v) for v in values)
    offset = 0
    for expected in values:
        value, offset = decode_uvarint(buf, offset)
        assert value == expected
    assert offset == len(buf)


def test_truncated_varint_is_rejected() -> None:
    raw = encode_uvarint(300)
    with pytest.raises(VarintError):
        decode_uvarint(raw[:1])
    with pytest.raises(VarintError):
        decode_uvarint(b"", 0)
    with pytest.raises(VarintError):
        decode_uvarint(b"\x01", 5)


def test_overlong_encoding_is_rejected() -> None:
    # 1 encoded in two bytes is not canonical; the log's CRCs rely on canonical
    # encodings and a non-canonical varint is a corruption signal.
    with pytest.raises(VarintError):
        decode_uvarint(b"\x81\x00")


def test_values_beyond_64_bits_are_rejected() -> None:
    with pytest.raises(VarintError):
        encode_uvarint(MAX_VALUE + 1)
    with pytest.raises(VarintError):
        encode_uvarint(-1)
    with pytest.raises(VarintError):
        decode_uvarint(b"\xff" * 10 + b"\x7f")
