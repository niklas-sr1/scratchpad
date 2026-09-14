"""Minimal diffs between two document states.

The history rewrite (:mod:`scratchpad.redaction`) replays every historical
state, transforms it, and has to express the difference between two consecutive
*transformed* states as document operations again.  That is all this module
does, and it is deliberately not a general diff library:

* One edit produced the difference between the two untransformed states, so the
  difference between the transformed ones is almost always one contiguous
  change too.  Trimming the common prefix and the common suffix therefore
  yields a single INSERT, DELETE or REPLACE in practice.
* :func:`difflib.SequenceMatcher` is only consulted when that single operation
  would be large *and* the two middles have enough in common for a multi
  operation diff to be smaller.  It is quadratic in the worst case, so it is
  never run on big inputs.

The affix search gallops (1, 4, 16, ... character blocks) instead of walking
character by character: comparing slices is a C memcmp, walking is a Python
loop.  Finding a prefix of length P costs O(P) memcpy in C and O(log P) Python
iterations, which is what makes "diff two 8 KB states 200000 times" affordable.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from scratchpad.core.ops import Op, OpKind

__all__ = ["common_prefix_len", "common_suffix_len", "diff_ops"]

#: A single REPLACE is always emitted while both middles are shorter than this.
MULTI_OP_THRESHOLD = 64
#: SequenceMatcher is quadratic; do not hand it more than this many characters.
MAX_SEQUENCE_MATCH = 20_000
#: Assumed per-record overhead when comparing a single op against several.
RECORD_OVERHEAD = 8

_FIRST_BLOCK = 16
_GROWTH = 4


def common_prefix_len(a: str, b: str, *, limit: int | None = None) -> int:
    """Length of the longest common prefix of ``a`` and ``b``.

    ``limit`` caps the answer.  Runs in O(result) C-level comparisons.
    """
    high = min(len(a), len(b))
    if limit is not None:
        high = min(high, max(limit, 0))
    if high <= 0:
        return 0

    low = 0                      # a[:low] == b[:low] is known
    block = _FIRST_BLOCK
    while low < high:            # gallop forward over equal blocks
        end = min(low + block, high)
        if a[low:end] == b[low:end]:
            low = end
            block *= _GROWTH
        else:
            high = end - 1       # the first difference is before `end`
            break
    while low < high:            # bisect the block that differed
        mid = (low + high + 1) // 2
        if a[low:mid] == b[low:mid]:
            low = mid
        else:
            high = mid - 1
    return low


def common_suffix_len(a: str, b: str, *, limit: int | None = None) -> int:
    """Length of the longest common suffix of ``a`` and ``b``.

    ``limit`` caps the answer, which is how the caller keeps the suffix from
    overlapping an already claimed prefix.
    """
    len_a, len_b = len(a), len(b)
    high = min(len_a, len_b)
    if limit is not None:
        high = min(high, max(limit, 0))
    if high <= 0:
        return 0

    low = 0
    block = _FIRST_BLOCK
    while low < high:
        end = min(low + block, high)
        if a[len_a - end : len_a - low] == b[len_b - end : len_b - low]:
            low = end
            block *= _GROWTH
        else:
            high = end - 1
            break
    while low < high:
        mid = (low + high + 1) // 2
        if a[len_a - mid : len_a - low] == b[len_b - mid : len_b - low]:
            low = mid
        else:
            high = mid - 1
    return low


def diff_ops(old: str, new: str, *, base_pos: int = 0) -> list[Op]:
    """Operations that turn ``old`` into ``new`` when applied in order.

    ``base_pos`` is added to every position, so a caller can diff a slice of a
    document and get operations in document coordinates.

    Returns an empty list when the two texts are equal.  Otherwise the common
    prefix and suffix are trimmed and, in the normal case, exactly one operation
    describes what is left: INSERT when nothing was removed, DELETE when nothing
    was added, REPLACE otherwise.  Only when that one operation would rewrite a
    long stretch is :class:`difflib.SequenceMatcher` asked for a cheaper
    multi-operation answer, and it is used only if it really is cheaper.
    """
    if old == new:
        return []

    prefix = common_prefix_len(old, new)
    suffix = common_suffix_len(old, new, limit=min(len(old), len(new)) - prefix)
    old_mid = old[prefix : len(old) - suffix]
    new_mid = new[prefix : len(new) - suffix]
    pos = base_pos + prefix

    if not old_mid:
        return [Op(OpKind.INSERT, pos, text=new_mid)]
    if not new_mid:
        return [Op(OpKind.DELETE, pos, old_text=old_mid)]

    single = [Op(OpKind.REPLACE, pos, text=new_mid, old_text=old_mid)]
    if min(len(old_mid), len(new_mid)) < MULTI_OP_THRESHOLD:
        return single
    if max(len(old_mid), len(new_mid)) > MAX_SEQUENCE_MATCH:
        return single

    multi = _sequence_match_ops(old_mid, new_mid, pos)
    if _cost(multi) < _cost(single):
        return multi
    return single


def _cost(ops: list[Op]) -> int:
    """Rough size on disk of a list of operations, for choosing between them."""
    return sum(len(op.text) + len(op.old_text) + RECORD_OVERHEAD for op in ops)


def _sequence_match_ops(old: str, new: str, base_pos: int) -> list[Op]:
    """Turn SequenceMatcher opcodes into ops that apply left to right.

    Each op is positioned in the document as it exists *after* the previous ops
    of the list have been applied, which is what "apply in order" means.
    """
    ops: list[Op] = []
    shift = 0
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        pos = base_pos + i1 + shift
        if tag == "insert":
            ops.append(Op(OpKind.INSERT, pos, text=new[j1:j2]))
        elif tag == "delete":
            ops.append(Op(OpKind.DELETE, pos, old_text=old[i1:i2]))
        else:
            ops.append(Op(OpKind.REPLACE, pos, text=new[j1:j2], old_text=old[i1:i2]))
        shift += (j2 - j1) - (i2 - i1)
    return ops
