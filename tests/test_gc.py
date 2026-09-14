"""Attachment garbage collection: what retained history still points at.

Spec section 23: deleting a token from the current scratchpad must not delete
the attachment, because historical states still reference it.  Only an object
that no retained state mentions may go.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scratchpad import gc as gc_module
from scratchpad import paths
from scratchpad.attachments import AttachmentStore
from scratchpad.config import Config
from scratchpad.core.clock import ManualClock
from scratchpad.core.ops import delete, insert
from scratchpad.core.store import ScratchpadStore
from scratchpad.gc import GcError, GcReport, collect_garbage
from scratchpad.tokens import TokenCodec, load_or_create_secret

SECOND = 1_000_000_000
WALL0 = 1_700_000_000 * SECOND
VERSION = "0.1.0-test"


class Bench:
    """A store, an attachment store and a codec over one data directory."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.codec = TokenCodec(load_or_create_secret(paths.secret_key(directory)))
        self.attachments = AttachmentStore(directory, self.codec)
        self.clock = ManualClock(wall=WALL0, mono=1000 * SECOND)
        self.store = ScratchpadStore.open(
            directory, Config(), app_version=VERSION, clock=self.clock
        )

    def insert(self, text: str, at: int | None = None) -> None:
        self.clock.advance(1_000_000)
        self.store.apply(insert(len(self.store.text) if at is None else at, text))

    def erase(self, start: int, length: int) -> None:
        self.clock.advance(1_000_000)
        self.store.apply(delete(start, self.store.text[start : start + length]))

    def make(self, payload: bytes = b"content\n"):
        return self.attachments.create_text(payload)

    def close(self) -> None:
        self.attachments.close()
        self.store.close()


@pytest.fixture
def bench(tmp_path: Path) -> Bench:
    instance = Bench(tmp_path / "data")
    yield instance
    instance.close()


# --- the basic decision -----------------------------------------------------


def test_an_unreferenced_attachment_is_deleted_with_its_blob(bench: Bench) -> None:
    orphan = bench.make(b"never referenced by anything\n")
    blob = bench.attachments.blob_path(orphan)
    assert blob.exists()

    report = collect_garbage(bench.store, bench.attachments, bench.codec)
    assert report.deleted_ids == [orphan.object_id]
    assert report.referenced_ids == 0
    assert report.scanned_events == bench.store.event_count
    assert bench.attachments.get(orphan.object_id) is None
    assert not blob.exists()


def test_a_referenced_attachment_is_kept(bench: Bench) -> None:
    keeper = bench.make()
    bench.insert("see " + keeper.token + " for details")
    report = collect_garbage(bench.store, bench.attachments, bench.codec)
    assert report.deleted_ids == []
    assert report.referenced_ids == 1
    assert bench.attachments.get(keeper.object_id) is not None


def test_a_token_that_was_deleted_again_is_still_referenced_by_history(bench: Bench) -> None:
    """Spec section 23: deleting the token from the current text keeps the object."""
    keeper = bench.make()
    bench.insert("x " + keeper.token + " y")
    bench.erase(2, len(keeper.token))
    assert keeper.token not in bench.store.text

    report = collect_garbage(bench.store, bench.attachments, bench.codec)
    assert report.deleted_ids == []
    assert bench.attachments.get(keeper.object_id) is not None


def test_a_token_assembled_by_two_inserts_is_found(bench: Bench) -> None:
    """A token typed or pasted in halves appears in no single event payload."""
    keeper = bench.make()
    half = len(keeper.token) // 2
    bench.insert("start ")
    bench.insert(keeper.token[:half])
    bench.insert(keeper.token[half:])
    assert keeper.token in bench.store.text
    for event in bench.store.history.events(0, bench.store.history.count):
        if event.op is not None:
            assert keeper.token not in event.op.text     # no payload holds it whole

    report = collect_garbage(bench.store, bench.attachments, bench.codec)
    assert report.deleted_ids == []
    assert report.referenced_ids == 1


def test_a_token_assembled_by_a_deletion_is_found(bench: Bench) -> None:
    """Deleting the separator between two halves also creates a token."""
    keeper = bench.make()
    half = len(keeper.token) // 2
    bench.insert("head " + keeper.token[:half] + "-" + keeper.token[half:] + " tail")
    bench.erase(5 + half, 1)
    assert keeper.token in bench.store.text
    bench.erase(0, len(bench.store.text))                # and then remove it again

    report = collect_garbage(bench.store, bench.attachments, bench.codec)
    assert report.deleted_ids == []
    assert bench.attachments.get(keeper.object_id) is not None


def test_a_token_that_only_ever_existed_for_one_keystroke_is_kept(bench: Bench) -> None:
    keeper = bench.make()
    bench.insert("z " * 2000)                            # a big document
    bench.insert(keeper.token + " ", at=2000)            # far from both ends
    bench.erase(2000, len(keeper.token) + 1)             # gone again one event later
    report = collect_garbage(bench.store, bench.attachments, bench.codec)
    assert report.deleted_ids == []


def test_a_token_inside_a_longer_alphanumeric_run_is_not_a_reference(bench: Bench) -> None:
    """Spec section 20: only a maximal run of exactly 22 characters is a candidate.

    A token pasted into the middle of a longer word never resolved in the UI
    either -- hovering it shows nothing -- so history does not reference it and
    the collector is free to remove the object.  This is the one case where the
    collector is not conservative, and it is deliberate: recognising tokens by
    any other rule would make ordinary text resolve as attachments.
    """
    orphan = bench.make()
    bench.insert("z" * 2000)
    bench.insert(orphan.token, at=1000)                  # swallowed by the run
    from scratchpad.tokens import find_candidates

    assert find_candidates(bench.store.text) == []
    report = collect_garbage(bench.store, bench.attachments, bench.codec)
    assert report.deleted_ids == [orphan.object_id]


def test_mixed_referenced_and_unreferenced(bench: Bench) -> None:
    kept = bench.make(b"kept\n")
    dropped = bench.make(b"dropped\n")
    also_kept = bench.make(b"also kept\n")
    bench.insert(kept.token + "\n")
    bench.insert(also_kept.token + "\n")
    bench.erase(0, len(kept.token))                      # only in history now

    report = collect_garbage(bench.store, bench.attachments, bench.codec)
    assert report.deleted_ids == [dropped.object_id]
    assert report.referenced_ids == 2
    assert bench.attachments.get(kept.object_id) is not None
    assert bench.attachments.get(also_kept.object_id) is not None
    assert bench.attachments.get(dropped.object_id) is None


def test_ordinary_alphanumeric_text_is_not_a_reference(bench: Bench) -> None:
    orphan = bench.make()
    bench.insert("Z" * 22 + " " + "0123456789abcdefghijkl" + " " + "x" * 30)
    report = collect_garbage(bench.store, bench.attachments, bench.codec)
    assert report.referenced_ids == 0
    assert report.deleted_ids == [orphan.object_id]


def test_a_reference_that_only_a_checkpoint_holds_is_honoured(bench: Bench) -> None:
    """Checkpoints are scanned in full as a second, independent source."""
    keeper = bench.make()
    bench.insert("in a checkpoint: " + keeper.token)
    bench.store.checkpoint()
    report = collect_garbage(bench.store, bench.attachments, bench.codec)
    assert report.deleted_ids == []


# --- dry runs and conservatism ----------------------------------------------


def test_a_dry_run_deletes_nothing(bench: Bench) -> None:
    orphan = bench.make()
    report = collect_garbage(bench.store, bench.attachments, bench.codec, dry_run=True)
    assert report.dry_run
    assert report.deleted_ids == [orphan.object_id]
    assert bench.attachments.get(orphan.object_id) is not None
    assert bench.attachments.blob_path(orphan).exists()


def test_a_failing_scan_deletes_nothing(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    orphan = bench.make()
    bench.insert("some text")

    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated failure half way through the replay")

    monkeypatch.setattr(gc_module, "apply_op", explode)
    with pytest.raises(RuntimeError):
        collect_garbage(bench.store, bench.attachments, bench.codec)
    assert bench.attachments.get(orphan.object_id) is not None


def test_an_unreadable_checkpoint_stops_the_collection(bench: Bench) -> None:
    orphan = bench.make()
    bench.insert("text")
    bench.store.checkpoint()
    for path in paths.checkpoints_dir(bench.directory).iterdir():
        path.write_bytes(b"not a checkpoint at all")
    with pytest.raises(GcError):
        collect_garbage(bench.store, bench.attachments, bench.codec)
    assert bench.attachments.get(orphan.object_id) is not None


def test_a_missing_installation_key_refuses_to_delete(bench: Bench) -> None:
    orphan = bench.make()
    with pytest.raises(GcError):
        collect_garbage(bench.store, bench.attachments, None)
    assert bench.attachments.get(orphan.object_id) is not None


def test_the_report_summarises_itself() -> None:
    line = GcReport(120, 2, [7, 9], False).summary()
    assert "120 events" in line
    assert "7, 9" in line
    assert GcReport(1, 0, [], True).summary().startswith("Attachment scan")
    assert "would delete" in GcReport(1, 0, [3], True).summary()


# --- interaction with redaction ---------------------------------------------


def test_purging_then_collecting_leaves_nothing_behind(bench: Bench) -> None:
    from scratchpad import redaction

    victim = bench.make(b"secret screenshot\n")
    keeper = bench.make(b"kept\n")
    bench.insert(victim.token + " " + keeper.token)
    bench.erase(0, len(victim.token) + 1)

    redaction.purge_attachment(bench.store, bench.attachments, victim.object_id)
    assert victim.token not in bench.store.text
    report = collect_garbage(bench.store, bench.attachments, bench.codec)
    assert report.deleted_ids == []                      # the keeper is still referenced
    assert report.referenced_ids == 1
    assert bench.attachments.get(victim.object_id) is None
