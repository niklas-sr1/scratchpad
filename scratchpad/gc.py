"""Attachment garbage collection: what history still points at, and what not.

Deleting a token from the current scratchpad does not delete the attachment --
historical states still reference it, and reconstructing them has to keep
working (spec section 23).  An attachment may only go once *no retained state*
mentions its token any more, which is what this module determines.

Finding the references
----------------------

A token is 22 characters of ordinary text in the document, and it can be
assembled by several edits: pasting one is a single INSERT, but typing one,
pasting it in two halves, or deleting the characters between two halves all
produce a token that no single event payload contains.  Scanning event payloads
is therefore wrong.  Instead the whole log is replayed and, after each edit, the
window ``[pos - 23, pos + len(inserted) + 23]`` of the resulting state is
scanned with :func:`scratchpad.tokens.find_candidates`.

That window is sufficient: any token occurrence in the new state that does not
overlap the characters the edit wrote existed in the previous state too and was
found then, and a token that does overlap them starts at most 21 characters
before ``pos`` and ends at most 21 characters after the written text.  The extra
character on each side keeps the "maximal run of exactly 22" rule intact at the
window edges, so a 23-character run is never mistaken for a token.

The current text and every checkpoint are scanned in full as well.  They are
redundant with the replay, and cheap; they exist so that a reference can only be
missed if the replay itself is wrong.

Conservatism
------------

Nothing is deleted unless the scan completed: the scan runs to the end before
the first row is touched, and any exception propagates with nothing deleted.  A
candidate that does not authenticate is ignored, an authenticating one in
another domain is ignored, and an object id that appears anywhere -- even in a
state that existed for a single keystroke -- keeps its attachment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from scratchpad import paths
from scratchpad.core import checkpoint as checkpoint_mod
from scratchpad.core.ops import apply_op
from scratchpad.tokens import DOMAIN_ATTACHMENT, TOKEN_LENGTH, TokenCodec, find_candidates

log = logging.getLogger(__name__)

__all__ = ["GcError", "GcReport", "collect_garbage"]

#: One character beyond the token width on each side, so that the maximality of
#: an alphanumeric run is still decidable inside the scanned window.
WINDOW_MARGIN = TOKEN_LENGTH + 1


class GcError(RuntimeError):
    """The scan could not be completed, so nothing was deleted."""


@dataclass
class GcReport:
    """What the collector saw and what it removed."""

    scanned_events: int = 0
    referenced_ids: int = 0
    deleted_ids: list[int] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        """One line for the log or a dialog."""
        verb = "would delete" if self.dry_run else "deleted"
        ids = ", ".join(str(i) for i in self.deleted_ids) or "none"
        return (
            f"Attachment scan: {self.scanned_events} events replayed, "
            f"{self.referenced_ids} attachment(s) still referenced by history, "
            f"{verb} {len(self.deleted_ids)} unreferenced attachment(s): {ids}"
        )


def _collect_from(text: str, codec: TokenCodec, referenced: set[int]) -> None:
    """Add every attachment id that ``text`` authenticates to ``referenced``."""
    for _start, _end, candidate in find_candidates(text):
        decoded = codec.decode(candidate)
        if decoded is None:
            continue
        object_id, domain = decoded
        if domain == DOMAIN_ATTACHMENT:
            referenced.add(object_id)


def _scan_history(store, codec: TokenCodec, referenced: set[int]) -> int:
    """Replay the log, scanning the neighbourhood of every edit.  Returns the count."""
    history = store.history
    doc = ""
    scanned = 0
    for event in history.events(0, history.count):
        scanned += 1
        op = event.op
        if op is None:
            continue
        doc = apply_op(doc, op)
        start = op.pos - WINDOW_MARGIN
        if start < 0:
            start = 0
        stop = op.pos + len(op.text) + WINDOW_MARGIN
        _collect_from(doc[start:stop], codec, referenced)
    return scanned


def _scan_checkpoints(data_dir: Path, codec: TokenCodec, referenced: set[int]) -> None:
    """Scan every checkpoint file in full (belt and braces; they are a cache)."""
    directory = paths.checkpoints_dir(data_dir)
    for _seq, path in checkpoint_mod.list_checkpoints(directory):
        try:
            checkpoint = checkpoint_mod.read_checkpoint(path)
        except Exception as exc:  # a damaged cache file must not delete anything
            raise GcError(f"checkpoint {path} is unreadable: {exc}") from exc
        _collect_from(checkpoint.text, codec, referenced)


def collect_garbage(store, attachments, codec: TokenCodec, *, dry_run: bool = False) -> GcReport:
    """Delete every attachment that no retained historical state references.

    Replays the whole event log, collects every attachment id that any state
    ever contained, and removes the rows (and, when unshared, the blobs) that
    are left over.  ``dry_run=True`` reports without deleting.

    Raises :class:`GcError`, or whatever the replay raised, without deleting
    anything if the scan cannot be completed.
    """
    if codec is None:
        raise GcError("the installation key is unavailable; refusing to delete attachments")
    store.flush(fsync=False)
    referenced: set[int] = set()
    scanned = _scan_history(store, codec, referenced)
    _collect_from(store.text, codec, referenced)
    _scan_checkpoints(Path(store.data_dir), codec, referenced)

    known = [attachment.object_id for attachment in attachments.list_all()]
    garbage = sorted(object_id for object_id in known if object_id not in referenced)
    report = GcReport(scanned, len(referenced), garbage, dry_run)
    if not dry_run:
        for object_id in garbage:
            attachments.delete(object_id)
    log.info("%s", report.summary())
    return report
