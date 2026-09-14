"""The history rewrite swap: what survives it, and what must not.

The dangerous half of a rewrite is not the log -- that one is renamed into place
atomically -- but the checkpoint directory next to it.  A checkpoint holds the
full document text at a sequence number, so a checkpoint that outlives the
rewrite hands exactly the text a redaction removed back to the next
reconstruction that hits its sequence number.
"""
from __future__ import annotations

from pathlib import Path

from scratchpad import paths
from scratchpad.config import Config
from scratchpad.core import checkpoint as checkpoint_mod
from scratchpad.core import eventlog, recovery
from scratchpad.core.eventlog import EventKind, EventLogWriter
from scratchpad.core.history import History
from scratchpad.core.ops import insert
from scratchpad.core.store import ScratchpadStore

SECOND = 1_000_000_000
WALL0 = 1_700_000_000 * SECOND
VERSION = "0.1.0-test"


def open_store(data_dir: Path, config: Config | None = None) -> ScratchpadStore:
    return ScratchpadStore.open(data_dir, config or Config(), app_version=VERSION)


def write_log(path: Path, texts: list[str], *, session_id: int = 7) -> None:
    """A complete little log: SESSION_START then one INSERT per text."""
    writer = EventLogWriter(path)
    writer.append(
        EventKind.SESSION_START, 0,
        eventlog.encode_session_start(session_id, WALL0, 1000, "rewrite"),
    )
    position = 0
    for text in texts:
        kind, payload = eventlog.encode_op(insert(position, text))
        writer.append(kind, SECOND, payload)
        position += len(text)
    writer.close()


def leftovers(data_dir: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in paths.history_dir(data_dir).iterdir()
        if entry.name.endswith((".old", ".rewrite")) or entry.name == recovery.JOURNAL_NAME
    )


def test_replace_history_without_new_checkpoints_drops_the_old_ones(data_dir: Path) -> None:
    """The reviewer's case: a rewrite with no staged checkpoint directory.

    The old checkpoints described the old log.  Kept next to the new one they
    answer an exact sequence number hit with pre-rewrite (that is: redacted)
    text, without anything ever validating them against the log.
    """
    store = open_store(data_dir)
    try:
        for word in ("secret ", "and more "):
            store.apply(insert(len(store.text), word))
        store.checkpoint()                       # a checkpoint of the secret text
        secret_seq = store.seq
        assert checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir))

        replacement = paths.history_dir(data_dir) / "rewritten.log"
        write_log(replacement, ["clean"])        # shorter, different, no checkpoints
        store.replace_history(replacement, None)

        assert store.text == "clean"
        for seq in range(store.event_count + 2):
            assert "secret" not in store.history.reconstruct_seq(seq)
        assert "secret" not in store.history.reconstruct_seq(secret_seq)
        assert leftovers(data_dir) == []
    finally:
        store.close()

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "clean"
        assert "secret" not in paths.events_log(data_dir).read_bytes().decode("utf-8", "replace")
        for _seq, path in checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir)):
            assert "secret" not in checkpoint_mod.read_checkpoint(path).text
    finally:
        reopened.close()


def test_a_journalled_swap_without_staged_checkpoints_still_clears_them(data_dir: Path) -> None:
    """Crash recovery must reach the same state as the store's own swap."""
    store = open_store(data_dir)
    store.apply(insert(0, "secret text"))
    store.close()
    checkpoints = paths.checkpoints_dir(data_dir)
    stale = checkpoint_mod.list_checkpoints(checkpoints)
    assert stale, "close() writes one"

    # A rewrite that journalled the swap but never staged a checkpoint directory.
    write_log(recovery.pending_log_path(data_dir), ["clean text"])
    recovery.write_journal(data_dir, "swap")

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "clean text"
        # The stale checkpoint sat at a sequence number the new log reuses, so
        # an exact hit returned its (pre-rewrite) text with nothing validating it.
        for seq in range(reopened.event_count + 2):
            assert "secret" not in reopened.history.reconstruct_seq(seq)
        surviving = checkpoint_mod.list_checkpoints(checkpoints)
        assert [path for _seq, path in surviving] != [path for _seq, path in stale]
        for _seq, path in surviving:
            assert "secret" not in checkpoint_mod.read_checkpoint(path).text
        assert leftovers(data_dir) == []
    finally:
        reopened.close()


def test_staged_checkpoints_are_installed(data_dir: Path) -> None:
    store = open_store(data_dir)
    store.apply(insert(0, "before"))
    store.close()

    write_log(recovery.pending_log_path(data_dir), ["after"])
    staged = recovery.pending_checkpoints_path(data_dir)
    checkpoint_mod.write_checkpoint(staged, 1, WALL0, "after")
    recovery.write_journal(data_dir, "swap")

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "after"
        assert reopened.history.reconstruct_seq(1) == "after"
        assert [seq for seq, _ in checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir))][0] == 1
        assert leftovers(data_dir) == []
    finally:
        reopened.close()


def test_replace_history_stages_an_empty_checkpoint_directory(data_dir: Path) -> None:
    """The protocol is uniform: there is always a checkpoints.rewrite to swap."""
    store = open_store(data_dir)
    try:
        store.apply(insert(0, "x"))
        store.checkpoint()
        staged: list[bool] = []
        real_finish = recovery.finish_rewrite

        def spy(dir_path: Path) -> None:
            staged.append(recovery.pending_checkpoints_path(dir_path).is_dir())
            real_finish(dir_path)

        recovery.finish_rewrite = spy
        try:
            replacement = paths.history_dir(data_dir) / "r.log"
            write_log(replacement, ["y"])
            store.replace_history(replacement, None)
        finally:
            recovery.finish_rewrite = real_finish
        assert staged == [True]
        assert store.text == "y"
    finally:
        store.close()


def test_an_uncommitted_rewrite_leaves_the_checkpoints_alone(data_dir: Path) -> None:
    """No journal means no swap: the live history, checkpoints included, stays."""
    store = open_store(data_dir)
    store.apply(insert(0, "keep me"))
    store.close()
    before = [seq for seq, _ in checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir))]
    assert before

    write_log(recovery.pending_log_path(data_dir), ["discard me"])
    checkpoint_mod.write_checkpoint(recovery.pending_checkpoints_path(data_dir), 1, WALL0, "discard me")

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "keep me"
        after = [seq for seq, _ in checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir))]
        assert after[:len(before)] == before
        assert leftovers(data_dir) == []
    finally:
        reopened.close()


def test_finish_rewrite_is_idempotent_and_never_loses_the_log(data_dir: Path) -> None:
    store = open_store(data_dir)
    store.apply(insert(0, "old"))
    store.close()
    write_log(recovery.pending_log_path(data_dir), ["new"])
    recovery.write_journal(data_dir, "swap")
    for _ in range(3):
        recovery.finish_rewrite(data_dir)

    history = History(paths.events_log(data_dir), paths.checkpoints_dir(data_dir))
    history.rebuild()
    assert history.reconstruct_seq(history.last_seq) == "new"
    assert paths.checkpoints_dir(data_dir).is_dir()
    assert leftovers(data_dir) == []
