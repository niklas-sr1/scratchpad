"""The document model: one text, one operation per mutation.

Positions are Unicode code point offsets, which is what ``Gtk.TextBuffer``
character offsets and Python string indexing both use.  Deletions store the
removed text (not only its length) so that history can be stepped backwards and
so that a corrupted replay is detected instead of silently producing garbage.

Every mutation of the editor buffer becomes exactly one :class:`Op`.  Undo and
redo are ordinary ops, not a rewind of history.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable


class OpKind(IntEnum):
    """Kind of a document operation."""

    INSERT = 1
    DELETE = 2
    REPLACE = 3


class OpError(ValueError):
    """An operation does not fit the document it is applied to."""


@dataclass(frozen=True, slots=True)
class Op:
    """A single document mutation at ``pos`` (code point offset)."""

    kind: OpKind
    pos: int
    text: str = ""       # INSERT: inserted text.  REPLACE: new text.
    old_text: str = ""   # DELETE: deleted text.   REPLACE: replaced text.

    def __post_init__(self) -> None:
        if self.pos < 0:
            raise OpError(f"negative position {self.pos}")

    @property
    def char_delta(self) -> int:
        """How much the document grows (or shrinks) when this op is applied."""
        return len(self.text) - len(self.old_text)

    @property
    def end(self) -> int:
        """Offset just past the affected range after the op has been applied."""
        return self.pos + len(self.text)

    def summary(self, limit: int = 40) -> str:
        """Short human readable form, for logs and reports."""
        payload = self.text if self.kind is not OpKind.DELETE else self.old_text
        if len(payload) > limit:
            payload = payload[:limit] + "..."
        return f"{self.kind.name}(pos={self.pos}, {payload!r})"


def insert(pos: int, text: str) -> Op:
    """Convenience constructor."""
    return Op(OpKind.INSERT, pos, text=text)


def delete(pos: int, old_text: str) -> Op:
    """Convenience constructor."""
    return Op(OpKind.DELETE, pos, old_text=old_text)


def replace(pos: int, old_text: str, text: str) -> Op:
    """Convenience constructor."""
    return Op(OpKind.REPLACE, pos, text=text, old_text=old_text)


def apply_op(doc: str, op: Op) -> str:
    """Return ``doc`` with ``op`` applied.

    Raises :class:`OpError` when the op does not fit: the position is out of
    range or the recorded ``old_text`` is not what the document holds there.
    """
    if op.kind is OpKind.INSERT:
        if op.pos > len(doc):
            raise OpError(f"INSERT at {op.pos} beyond end of {len(doc)}-char document")
        if not op.text:
            return doc
        return doc[: op.pos] + op.text + doc[op.pos :]

    end = op.pos + len(op.old_text)
    if end > len(doc):
        raise OpError(
            f"{op.kind.name} at {op.pos}+{len(op.old_text)} beyond end of {len(doc)}-char document"
        )
    if doc[op.pos : end] != op.old_text:
        raise OpError(
            f"{op.kind.name} at {op.pos}: document holds {doc[op.pos:end]!r}, "
            f"operation expected {op.old_text!r}"
        )
    if op.kind is OpKind.DELETE:
        return doc[: op.pos] + doc[end:]
    if op.kind is OpKind.REPLACE:
        return doc[: op.pos] + op.text + doc[end:]
    raise OpError(f"unknown op kind {op.kind!r}")


def invert_op(op: Op) -> Op:
    """The operation that undoes ``op`` (for backward stepping)."""
    if op.kind is OpKind.INSERT:
        return Op(OpKind.DELETE, op.pos, old_text=op.text)
    if op.kind is OpKind.DELETE:
        return Op(OpKind.INSERT, op.pos, text=op.old_text)
    if op.kind is OpKind.REPLACE:
        return Op(OpKind.REPLACE, op.pos, text=op.old_text, old_text=op.text)
    raise OpError(f"unknown op kind {op.kind!r}")


def replay_ops(doc: str, ops: Iterable[Op]) -> str:
    """Apply many ops to ``doc``, coalescing adjacent ones first.

    Plain ``apply_op`` per event copies the whole document every time, which
    turns "replay the 2000 events since the last checkpoint" into a quadratic
    amount of memcpy.  Runs of ops that touch consecutive positions -- exactly
    what typing and holding backspace produce -- are merged into one op, so the
    common replay costs a couple of copies instead of thousands.  The result is
    identical to applying the ops one by one, including the validation errors.
    """
    pending_kind: OpKind | None = None
    pending_pos = 0
    pending_parts: list[str] = []
    pending_len = 0

    def flush(text: str) -> str:
        nonlocal pending_kind, pending_parts, pending_len
        if pending_kind is None:
            return text
        payload = "".join(pending_parts)
        if pending_kind is OpKind.INSERT:
            merged = Op(OpKind.INSERT, pending_pos, text=payload)
        else:
            merged = Op(OpKind.DELETE, pending_pos, old_text=payload)
        pending_kind = None
        pending_parts = []
        pending_len = 0
        return apply_op(text, merged)

    for op in ops:
        kind = op.kind
        if kind is OpKind.INSERT:
            if pending_kind is OpKind.INSERT and op.pos == pending_pos + pending_len:
                pending_parts.append(op.text)
                pending_len += len(op.text)
                continue
            doc = flush(doc)
            pending_kind = OpKind.INSERT
            pending_pos = op.pos
            pending_parts = [op.text]
            pending_len = len(op.text)
        elif kind is OpKind.DELETE:
            if pending_kind is OpKind.DELETE:
                if op.pos == pending_pos:
                    # forward delete: the next chunk sat behind the previous one
                    pending_parts.append(op.old_text)
                    pending_len += len(op.old_text)
                    continue
                if op.pos + len(op.old_text) == pending_pos:
                    # backspace: the next chunk sat in front of the previous one
                    pending_parts.insert(0, op.old_text)
                    pending_pos = op.pos
                    pending_len += len(op.old_text)
                    continue
            doc = flush(doc)
            pending_kind = OpKind.DELETE
            pending_pos = op.pos
            pending_parts = [op.old_text]
            pending_len = len(op.old_text)
        else:
            doc = flush(doc)
            doc = apply_op(doc, op)
    return flush(doc)
