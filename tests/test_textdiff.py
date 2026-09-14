"""Minimal diffs: correctness (hypothesis), minimality and the affix search."""
from __future__ import annotations

import random

from hypothesis import given, settings, strategies as st

from scratchpad.core.ops import OpKind, apply_op
from scratchpad.textdiff import common_prefix_len, common_suffix_len, diff_ops

ALPHABET = "ab \né"


def apply_all(text: str, ops) -> str:
    """Apply ops in order, which is the contract diff_ops promises."""
    for op in ops:
        text = apply_op(text, op)
    return text


# --- correctness ------------------------------------------------------------


@given(st.text(alphabet=ALPHABET, max_size=60), st.text(alphabet=ALPHABET, max_size=60))
@settings(max_examples=400)
def test_applying_the_ops_in_order_yields_the_new_text(old: str, new: str) -> None:
    assert apply_all(old, diff_ops(old, new)) == new


@given(st.text(alphabet=ALPHABET, max_size=60))
@settings(max_examples=100)
def test_equal_texts_produce_no_ops(text: str) -> None:
    assert diff_ops(text, text) == []


@given(
    st.text(alphabet=ALPHABET, max_size=20),
    st.text(alphabet=ALPHABET, max_size=30),
    st.text(alphabet=ALPHABET, max_size=30),
    st.text(alphabet=ALPHABET, max_size=20),
)
@settings(max_examples=300)
def test_base_pos_places_the_ops_in_document_coordinates(
    head: str, old: str, new: str, tail: str
) -> None:
    ops = diff_ops(old, new, base_pos=len(head))
    assert apply_all(head + old + tail, ops) == head + new + tail


@given(
    st.text(alphabet="abc", max_size=200),
    st.text(alphabet="abc", max_size=200),
)
@settings(max_examples=200)
def test_long_texts_round_trip_too(old: str, new: str) -> None:
    assert apply_all(old, diff_ops(old, new)) == new


def test_random_large_documents_round_trip() -> None:
    rng = random.Random(7)
    for _ in range(50):
        old = "".join(rng.choice("abcdef\n") for _ in range(rng.randrange(500, 4000)))
        new = list(old)
        for _ in range(rng.randrange(1, 6)):
            where = rng.randrange(0, len(new) + 1)
            if rng.random() < 0.5:
                new[where:where] = list("inserted text")
            else:
                del new[where : where + rng.randrange(1, 40)]
        new_text = "".join(new)
        assert apply_all(old, diff_ops(old, new_text)) == new_text


# --- minimality -------------------------------------------------------------


def test_a_pure_insertion_is_one_insert_op() -> None:
    ops = diff_ops("hello world", "hello brave world")
    assert len(ops) == 1
    assert ops[0].kind is OpKind.INSERT
    assert ops[0].pos == 6
    assert ops[0].text == "brave "


def test_a_pure_deletion_is_one_delete_op() -> None:
    ops = diff_ops("hello brave world", "hello world")
    assert len(ops) == 1
    assert ops[0].kind is OpKind.DELETE
    assert ops[0].pos == 6
    assert ops[0].old_text == "brave "


def test_a_replacement_is_one_replace_op() -> None:
    ops = diff_ops("hello brave world", "hello timid world")
    assert len(ops) == 1
    assert ops[0].kind is OpKind.REPLACE
    assert ops[0].pos == 6
    assert ops[0].old_text == "brave"
    assert ops[0].text == "timid"


def test_deleting_everything_is_one_delete_op() -> None:
    ops = diff_ops("everything", "")
    assert len(ops) == 1
    assert ops[0].kind is OpKind.DELETE
    assert ops[0].pos == 0


def test_two_far_apart_changes_fall_back_to_several_ops() -> None:
    old = "a" * 80 + "MIDDLE" * 50 + "b" * 80
    new = "c" * 80 + "MIDDLE" * 50 + "d" * 80
    ops = diff_ops(old, new)
    assert len(ops) > 1
    assert apply_all(old, ops) == new
    payload = sum(len(op.text) + len(op.old_text) for op in ops)
    assert payload < len(old) + len(new)


def test_a_small_scattered_change_stays_one_op() -> None:
    # Both middles are short, so the single REPLACE wins and difflib is not run.
    ops = diff_ops("abcdefghij", "ajcdefghib")
    assert len(ops) == 1
    assert ops[0].kind is OpKind.REPLACE


# --- affix search -----------------------------------------------------------


def test_common_prefix_and_suffix_lengths() -> None:
    assert common_prefix_len("", "") == 0
    assert common_prefix_len("abc", "abd") == 2
    assert common_prefix_len("abc", "abc") == 3
    assert common_prefix_len("abc", "xyz") == 0
    assert common_prefix_len("abcdef", "abcdef", limit=2) == 2
    assert common_suffix_len("abc", "xbc") == 2
    assert common_suffix_len("abc", "abc") == 3
    assert common_suffix_len("abc", "xyz") == 0
    assert common_suffix_len("aaaa", "aaaa", limit=1) == 1


@given(st.text(max_size=80), st.text(max_size=80))
@settings(max_examples=300)
def test_affix_lengths_agree_with_a_naive_implementation(a: str, b: str) -> None:
    naive_prefix = 0
    for left, right in zip(a, b):
        if left != right:
            break
        naive_prefix += 1
    naive_suffix = 0
    for left, right in zip(reversed(a), reversed(b)):
        if left != right:
            break
        naive_suffix += 1
    assert common_prefix_len(a, b) == naive_prefix
    assert common_suffix_len(a, b) == naive_suffix


def test_the_affix_search_handles_long_equal_runs() -> None:
    a = "x" * 100_000 + "left"
    b = "x" * 100_000 + "right"
    assert common_prefix_len(a, b) == 100_000
    assert common_suffix_len("tail" + "y" * 50_000, "TAIL" + "y" * 50_000) == 50_000
