"""Document model: apply, invert, replay."""
from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from scratchpad.core.ops import (
    Op,
    OpError,
    OpKind,
    apply_op,
    delete,
    insert,
    invert_op,
    replace,
    replay_ops,
)


def test_insert_delete_replace() -> None:
    assert apply_op("", insert(0, "hello")) == "hello"
    assert apply_op("hello", insert(5, " world")) == "hello world"
    assert apply_op("hello", insert(0, ">")) == ">hello"
    assert apply_op("hello", delete(1, "ell")) == "ho"
    assert apply_op("hello", replace(0, "hello", "bye")) == "bye"


def test_positions_are_code_points_not_bytes() -> None:
    doc = "aé\U0001f600b"          # a, e-acute, emoji, b
    assert len(doc) == 4
    assert apply_op(doc, insert(3, "X")) == "aé\U0001f600Xb"
    assert apply_op(doc, delete(2, "\U0001f600")) == "aéb"


def test_operations_that_do_not_fit_are_rejected() -> None:
    with pytest.raises(OpError):
        apply_op("abc", insert(4, "x"))
    with pytest.raises(OpError):
        apply_op("abc", delete(1, "xy"))       # document holds "bc"
    with pytest.raises(OpError):
        apply_op("abc", delete(2, "cd"))       # runs past the end
    with pytest.raises(OpError):
        apply_op("abc", replace(0, "x", "y"))
    with pytest.raises(OpError):
        Op(OpKind.INSERT, -1, text="x")


def test_invert_round_trips() -> None:
    doc = "hello world"
    for op in (insert(5, "XY"), delete(0, "hello"), replace(6, "world", "there")):
        after = apply_op(doc, op)
        assert apply_op(after, invert_op(op)) == doc


@given(st.text(max_size=200), st.text(max_size=20), st.integers(min_value=0, max_value=200))
def test_insert_then_invert_is_identity(doc: str, text: str, pos: int) -> None:
    pos = min(pos, len(doc))
    op = insert(pos, text)
    assert apply_op(apply_op(doc, op), invert_op(op)) == doc


def test_char_delta() -> None:
    assert insert(0, "abc").char_delta == 3
    assert delete(0, "abc").char_delta == -3
    assert replace(0, "abc", "xy").char_delta == -1


# --- replay_ops -------------------------------------------------------------


def _ops_for_typing(text: str, start: int = 0) -> list[Op]:
    return [insert(start + i, ch) for i, ch in enumerate(text)]


def test_replay_matches_one_by_one_application() -> None:
    ops = _ops_for_typing("hello world")
    ops += [delete(10, "d"), delete(9, "l"), insert(9, "D!")]
    ops += [replace(0, "hello", "HELLO")]
    step = ""
    for op in ops:
        step = apply_op(step, op)
    assert replay_ops("", ops) == step


def test_replay_coalesces_typing_and_backspacing() -> None:
    doc = replay_ops("", _ops_for_typing("abcdef"))
    assert doc == "abcdef"
    backspaces = [delete(5, "f"), delete(4, "e"), delete(3, "d")]
    assert replay_ops(doc, backspaces) == "abc"
    forward = [delete(0, "a"), delete(0, "b")]
    assert replay_ops("abc", forward) == "c"


def test_replay_validates_like_apply_op() -> None:
    with pytest.raises(OpError):
        replay_ops("abc", [insert(0, "x"), delete(0, "zz")])


@given(st.lists(st.integers(min_value=0, max_value=3), min_size=0, max_size=60))
def test_replay_equals_sequential_application(choices: list[int]) -> None:
    doc = "seed text"
    ops: list[Op] = []
    step = doc
    for choice in choices:
        if choice == 0 or not step:
            op = insert(len(step), "x")
        elif choice == 1:
            op = insert(0, "y")
        elif choice == 2:
            op = delete(len(step) - 1, step[-1])
        else:
            op = replace(0, step[0], "Z")
        ops.append(op)
        step = apply_op(step, op)
    assert replay_ops(doc, ops) == step
