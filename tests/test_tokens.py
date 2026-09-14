"""Tests for the token codec: construction, canonical Base62, candidates, key handling."""

from __future__ import annotations

import os
import random
import secrets
import stat
import string
import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scratchpad.tokens import (
    ALPHABET,
    DOMAIN_ATTACHMENT,
    MAX_OBJECT_ID,
    SECRET_SIZE,
    TOKEN_LENGTH,
    TokenCodec,
    base62_decode_fixed,
    base62_encode_fixed,
    candidate_at,
    derive_keys,
    find_candidates,
    load_or_create_secret,
)

# A fixed secret keeps the statistical assertions (shared prefixes, accidental validation)
# deterministic across runs instead of flaky at ~1e-4.
FIXED_SECRET = bytes(range(32))
OTHER_SECRET = bytes(range(100, 132))

CODEC = TokenCodec(FIXED_SECRET)
OTHER = TokenCodec(OTHER_SECRET)
ALNUM = string.digits + string.ascii_letters


# ----------------------------------------------------------------- base62 canonical


def test_base62_fixed_width_and_alphabet() -> None:
    assert base62_encode_fixed(0) == "0" * TOKEN_LENGTH
    assert base62_encode_fixed(1) == "0" * (TOKEN_LENGTH - 1) + "1"
    assert base62_encode_fixed(61) == "0" * (TOKEN_LENGTH - 1) + "z"
    assert base62_encode_fixed(62) == "0" * (TOKEN_LENGTH - 2) + "10"
    assert len(base62_encode_fixed((1 << 128) - 1)) == TOKEN_LENGTH


@given(st.integers(min_value=0, max_value=(1 << 128) - 1))
def test_base62_round_trip(value: int) -> None:
    encoded = base62_encode_fixed(value)
    assert len(encoded) == TOKEN_LENGTH
    assert set(encoded) <= set(ALPHABET)
    assert base62_decode_fixed(encoded) == value


def test_base62_rejects_non_canonical_and_out_of_range() -> None:
    assert base62_decode_fixed("z" * TOKEN_LENGTH) is None  # 62**22 - 1 >= 2**128
    # The first string above the representable range.
    limit = 1 << 128
    digits = []
    v = limit
    while v:
        v, r = divmod(v, 62)
        digits.append(ALPHABET[r])
    first_over = "".join(reversed(digits)).rjust(TOKEN_LENGTH, "0")
    assert len(first_over) == TOKEN_LENGTH
    assert base62_decode_fixed(first_over) is None
    assert base62_decode_fixed(base62_encode_fixed(limit - 1)) == limit - 1
    # Wrong length and foreign characters.
    assert base62_decode_fixed("0" * (TOKEN_LENGTH - 1)) is None
    assert base62_decode_fixed("0" * (TOKEN_LENGTH + 1)) is None
    assert base62_decode_fixed("0" * (TOKEN_LENGTH - 1) + "-") is None
    assert base62_decode_fixed("0" * (TOKEN_LENGTH - 1) + "é") is None
    with pytest.raises(ValueError):
        base62_encode_fixed(1 << 128)
    with pytest.raises(ValueError):
        base62_encode_fixed(-1)


# ------------------------------------------------------------------- codec basics


def test_token_shape() -> None:
    token = CODEC.encode(1, DOMAIN_ATTACHMENT)
    assert len(token) == TOKEN_LENGTH == 22
    assert set(token) <= set(ALPHABET)


@settings(max_examples=400)
@given(
    object_id=st.integers(min_value=0, max_value=MAX_OBJECT_ID),
    domain=st.integers(min_value=0, max_value=255),
)
def test_round_trip_all_ids_and_domains(object_id: int, domain: int) -> None:
    token = CODEC.encode(object_id, domain)
    assert len(token) == TOKEN_LENGTH
    assert set(token) <= set(ALPHABET)
    assert CODEC.decode(token) == (object_id, domain)


def test_round_trip_boundaries() -> None:
    for object_id in (0, 1, 255, 256, MAX_OBJECT_ID - 1, MAX_OBJECT_ID):
        for domain in (0, 1, 255):
            assert CODEC.decode(CODEC.encode(object_id, domain)) == (object_id, domain)


def test_encode_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        CODEC.encode(MAX_OBJECT_ID + 1, DOMAIN_ATTACHMENT)
    with pytest.raises(ValueError):
        CODEC.encode(-1, DOMAIN_ATTACHMENT)
    with pytest.raises(ValueError):
        CODEC.encode(1, 256)


def test_encoding_is_deterministic_and_injective() -> None:
    tokens = {CODEC.encode(i, DOMAIN_ATTACHMENT) for i in range(500)}
    assert len(tokens) == 500
    assert CODEC.encode(7, DOMAIN_ATTACHMENT) == CODEC.encode(7, DOMAIN_ATTACHMENT)
    assert CODEC.encode(7, 0) != CODEC.encode(7, 1)


# ------------------------------------------------- acceptance criterion 11: foreign keys


@settings(max_examples=200)
@given(object_id=st.integers(min_value=0, max_value=MAX_OBJECT_ID))
def test_ac11_other_installation_tokens_do_not_validate(object_id: int) -> None:
    """Spec 39.11: tokens issued by another installation do not normally validate."""
    assert OTHER.decode(CODEC.encode(object_id, DOMAIN_ATTACHMENT)) is None
    assert CODEC.decode(OTHER.encode(object_id, DOMAIN_ATTACHMENT)) is None


def test_many_foreign_tokens_never_validate() -> None:
    foreign = TokenCodec(secrets.token_bytes(32))
    assert not [i for i in range(5000) if CODEC.decode(foreign.encode(i, DOMAIN_ATTACHMENT))]


# ------------------------------------------- acceptance criterion 10: random strings


def test_ac10_random_strings_do_not_validate() -> None:
    """Spec 39.10: arbitrary pasted alphanumeric strings almost certainly do not validate."""
    rng = random.Random(0xC0FFEE)
    started = time.perf_counter()
    for _ in range(100_000):
        candidate = "".join(rng.choices(ALNUM, k=TOKEN_LENGTH))
        assert CODEC.decode(candidate) is None
    elapsed = time.perf_counter() - started
    assert elapsed < 10.0, f"100k decodes took {elapsed:.2f}s"


@given(st.text(alphabet=ALNUM, min_size=TOKEN_LENGTH, max_size=TOKEN_LENGTH))
def test_random_candidates_do_not_validate(candidate: str) -> None:
    assert CODEC.decode(candidate) is None


@given(st.text(min_size=0, max_size=40))
def test_arbitrary_text_never_crashes_decode(text: str) -> None:
    assert CODEC.decode(text) is None or len(text) == TOKEN_LENGTH


@settings(max_examples=300)
@given(
    object_id=st.integers(min_value=0, max_value=MAX_OBJECT_ID),
    index=st.integers(min_value=0, max_value=TOKEN_LENGTH - 1),
    replacement=st.sampled_from(ALPHABET),
)
def test_single_character_mutation_invalidates(object_id: int, index: int, replacement: str) -> None:
    token = CODEC.encode(object_id, DOMAIN_ATTACHMENT)
    if token[index] == replacement:
        return
    mutated = token[:index] + replacement + token[index + 1 :]
    assert CODEC.decode(mutated) is None


def test_every_single_character_mutation_of_one_token_invalidates() -> None:
    token = CODEC.encode(424242, DOMAIN_ATTACHMENT)
    for index in range(TOKEN_LENGTH):
        for replacement in ALPHABET:
            if replacement == token[index]:
                continue
            assert CODEC.decode(token[:index] + replacement + token[index + 1 :]) is None


# ------------------------------------------ acceptance criterion 13: visual unrelatedness


def _common_prefix(a: str, b: str) -> int:
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return n


def _common_suffix(a: str, b: str) -> int:
    return _common_prefix(a[::-1], b[::-1])


def test_ac13_sequential_ids_look_unrelated() -> None:
    """Spec 39.13: sequentially created attachments receive visually unrelated tokens."""
    tokens = [CODEC.encode(i, DOMAIN_ATTACHMENT) for i in range(1, 51)]
    worst_prefix = worst_suffix = 0
    for i, a in enumerate(tokens):
        for b in tokens[i + 1 :]:
            worst_prefix = max(worst_prefix, _common_prefix(a, b))
            worst_suffix = max(worst_suffix, _common_suffix(a, b))
    assert worst_prefix <= 3, f"shared prefix of {worst_prefix} characters"
    assert worst_suffix <= 3, f"shared suffix of {worst_suffix} characters"
    # Consecutive ids in particular share nothing structural.
    for a, b in zip(tokens, tokens[1:]):
        assert _common_prefix(a, b) <= 2
        assert _common_suffix(a, b) <= 2


def test_sequential_ids_differ_in_most_positions() -> None:
    a = CODEC.encode(437, DOMAIN_ATTACHMENT)
    b = CODEC.encode(438, DOMAIN_ATTACHMENT)
    same = sum(1 for x, y in zip(a, b) if x == y)
    assert same <= 6, f"{a} and {b} agree in {same} of {TOKEN_LENGTH} positions"


def test_distribution_of_characters_is_spread() -> None:
    tokens = [CODEC.encode(i, DOMAIN_ATTACHMENT) for i in range(1, 500)]
    # The leading Base62 digit of a uniform 128 bit value only ranges over 0..7
    # (2**128 / 62**21 is about 7.8), which is a property of fixed width Base62 and not
    # of the id: it carries about 3 bits. Every other position is uniform over 62.
    assert len({t[0] for t in tokens}) >= 5
    for position in (1, 2, 10, 21):
        assert len({t[position] for t in tokens}) > 50, position


# ------------------------------------------------------------------ key derivation


def test_derive_keys_is_deterministic_and_separated() -> None:
    prp_a, mac_a = derive_keys(FIXED_SECRET)
    prp_b, mac_b = derive_keys(FIXED_SECRET)
    assert (prp_a, mac_a) == (prp_b, mac_b)
    assert len(prp_a) == 16 and len(mac_a) == 32
    assert prp_a != mac_a[:16]
    other_prp, other_mac = derive_keys(OTHER_SECRET)
    assert other_prp != prp_a and other_mac != mac_a


def test_derive_keys_rejects_short_secret() -> None:
    with pytest.raises(ValueError):
        derive_keys(b"short")


def test_load_or_create_secret(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "secret.key"
    secret = load_or_create_secret(path)
    assert len(secret) == SECRET_SIZE
    assert path.exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, oct(mode)
    assert load_or_create_secret(path) == secret  # stable, never regenerated
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".")]


def test_load_or_create_secret_rejects_truncated_file(tmp_path: Path) -> None:
    path = tmp_path / "secret.key"
    path.write_bytes(b"tiny")
    with pytest.raises(ValueError):
        load_or_create_secret(path)


def test_two_installations_get_different_secrets(tmp_path: Path) -> None:
    a = load_or_create_secret(tmp_path / "a" / "secret.key")
    b = load_or_create_secret(tmp_path / "b" / "secret.key")
    assert a != b


# -------------------------------------------------------------- candidate scanning


def test_find_candidates_basic() -> None:
    token = CODEC.encode(1, DOMAIN_ATTACHMENT)
    text = f"- investigate CAN timeout\n  {token}\n"
    assert find_candidates(text) == [(28, 28 + TOKEN_LENGTH, token)]
    assert TokenCodec.find_candidates(text) == find_candidates(text)


def test_find_candidates_edges_and_delimiters() -> None:
    token = "A" * 22
    assert find_candidates(token) == [(0, 22, token)]
    assert find_candidates(f"{token} ") == [(0, 22, token)]
    assert find_candidates(f" {token}") == [(1, 23, token)]
    for left, right in (("_", "_"), ("-", "-"), ("(", ")"), ("\n", "\n"), ("é", "é"), (".", ",")):
        text = f"{left}{token}{right}"
        assert find_candidates(text) == [(1, 23, token)], text


def test_find_candidates_rejects_longer_runs() -> None:
    """Spec section 20: a longer alphanumeric run contains no candidate."""
    token = CODEC.encode(5, DOMAIN_ATTACHMENT)
    assert find_candidates("abc" + token + "def") == []
    assert find_candidates(token + "x") == []
    assert find_candidates("x" + token) == []
    assert find_candidates("A" * 23) == []
    assert find_candidates("A" * 21) == []
    assert find_candidates("A" * 44) == []


def test_find_candidates_multiple() -> None:
    a, b = CODEC.encode(1), CODEC.encode(2)
    text = f"{a} middle {b}\ntail {a}x\n"
    found = find_candidates(text)
    assert [c for _, _, c in found] == [a, b]
    for start, end, cand in found:
        assert text[start:end] == cand


def test_candidate_at_positions() -> None:
    token = CODEC.encode(9)
    text = f"xx {token} yy"
    start, end = 3, 3 + TOKEN_LENGTH
    for pos in range(start, end + 1):  # end included: cursor right behind the token
        assert candidate_at(text, pos) == (start, end, token)
    assert candidate_at(text, start - 1) is None
    assert candidate_at(text, end + 1) is None
    assert candidate_at(text, 0) is None
    assert candidate_at(text, len(text)) is None
    assert candidate_at(text, len(text) + 5) is None
    assert candidate_at(text, -1) is None
    assert TokenCodec.candidate_at(text, start) == (start, end, token)


def test_candidate_at_token_at_both_ends() -> None:
    token = CODEC.encode(11)
    assert candidate_at(token, 0) == (0, 22, token)
    assert candidate_at(token, 22) == (0, 22, token)
    assert candidate_at(token, 11) == (0, 22, token)
    text = "pad " + token
    assert candidate_at(text, len(text)) == (4, len(text), token)


def test_candidate_at_rejects_long_runs() -> None:
    text = "y" * 100
    for pos in (0, 1, 50, 99, 100):
        assert candidate_at(text, pos) is None
    text = " " + "y" * 23 + " "
    for pos in range(0, 26):
        assert candidate_at(text, pos) is None


def test_candidate_at_matches_find_candidates() -> None:
    rng = random.Random(7)
    parts = []
    for i in range(200):
        parts.append(CODEC.encode(i) if i % 3 else "".join(rng.choices(ALNUM, k=rng.randint(1, 30))))
        parts.append(rng.choice([" ", "\n", ", ", "_", "-"]))
    text = "".join(parts)
    expected = {(s, e) for s, e, _ in find_candidates(text)}
    for pos in range(len(text) + 1):
        got = candidate_at(text, pos)
        if got is None:
            assert not any(s <= pos <= e for s, e in expected)
        else:
            assert (got[0], got[1]) in expected
            assert got[0] <= pos <= got[1]


# ------------------------------------------------------------------- performance


def _big_text(tokens: list[str]) -> str:
    chunks = []
    filler = "lorem ipsum dolor sit amet consectetur 0123456789abcdef " * 2
    for i, token in enumerate(tokens):
        chunks.append(f"{i:06d} {filler}{token} tail-{i}\n")
    return "".join(chunks)


def test_find_candidates_is_fast_on_one_megabyte() -> None:
    tokens = [CODEC.encode(i) for i in range(1, 9001)]
    text = _big_text(tokens)
    assert len(text) > 1_000_000, len(text)
    started = time.perf_counter()
    found = find_candidates(text)
    elapsed = time.perf_counter() - started
    assert len(found) == len(tokens)
    assert elapsed < 0.5, f"find_candidates on {len(text)} chars took {elapsed:.3f}s"


def test_candidate_at_is_fast_on_one_megabyte() -> None:
    text = _big_text([CODEC.encode(i) for i in range(1, 9001)])
    positions = list(range(0, len(text), 97))
    started = time.perf_counter()
    for pos in positions:
        candidate_at(text, pos)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, f"{len(positions)} candidate_at calls took {elapsed:.3f}s"


def test_decode_throughput() -> None:
    tokens = [CODEC.encode(i) for i in range(2000)]
    started = time.perf_counter()
    for token in tokens:
        assert CODEC.decode(token) is not None
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"2000 decodes took {elapsed:.3f}s"
