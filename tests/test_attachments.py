"""Tests for the attachment store: metadata, blobs, dedup, deletion and resolution."""

from __future__ import annotations

import codecs
import hashlib
import os
import random
import sqlite3
import stat
import string
import struct
import time
import tracemalloc
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from scratchpad import attachments as attachments_module
from scratchpad.attachments import (
    KEY_FALLBACK_DETAIL,
    Attachment,
    AttachmentKind,
    AttachmentStore,
    ResolutionState,
    image_dimensions,
)
from scratchpad.tokens import (
    ALPHABET,
    DOMAIN_ATTACHMENT,
    TOKEN_LENGTH,
    TokenCodec,
    find_candidates,
    load_or_create_secret,
)

ALNUM = string.digits + string.ascii_letters


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AttachmentStore]:
    codec = TokenCodec(load_or_create_secret(tmp_path / "secret.key"))
    s = AttachmentStore(tmp_path, codec)
    yield s
    s.close()


def blob_files(data_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in (data_dir / "attachments" / "blobs").rglob("*")
        if p.is_file() and not p.name.startswith(".tmp-")
    )


# ------------------------------------------------------------------ image fixtures


def png_bytes(width: int, height: int) -> bytes:
    """A real, complete PNG of the given size (solid black, RGB)."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def gif_bytes(width: int, height: int) -> bytes:
    """A GIF89a header plus logical screen descriptor and trailer."""
    return (
        b"GIF89a"
        + struct.pack("<HH", width, height)
        + b"\xf0\x00\x00"
        + b"\x00\x00\x00\xff\xff\xff"  # 2 entry global colour table
        + b";"
    )


def jpeg_bytes(width: int, height: int) -> bytes:
    """A hand-crafted JFIF stream with APP0, DQT, a fill byte run and SOF0."""
    app0 = b"JFIF\x00" + b"\x01\x01\x00" + struct.pack(">HH", 1, 1) + b"\x00\x00"
    out = b"\xff\xd8"
    out += b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0
    out += b"\xff\xff"  # marker prefix plus a fill byte, which a parser must skip
    out += b"\xdb" + struct.pack(">H", 2 + 65) + b"\x00" + bytes(64)  # DQT
    sof = b"\x08" + struct.pack(">HH", height, width) + b"\x01" + b"\x01\x11\x00"
    out += b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    out += b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00"  # SOS
    out += b"\xff\xd9"
    return out


def webp_vp8x_bytes(width: int, height: int) -> bytes:
    body = b"\x10\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = b"VP8X" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def webp_vp8_bytes(width: int, height: int) -> bytes:
    body = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    chunk = b"VP8 " + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def webp_vp8l_bytes(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    body = b"\x2f" + struct.pack("<I", bits)
    chunk = b"VP8L" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


# --------------------------------------------------------------------- basic store


def test_database_is_wal(store: AttachmentStore, tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "attachments" / "attachments.sqlite")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_table_columns_match_the_contract(store: AttachmentStore) -> None:
    conn = sqlite3.connect(store.root / "attachments.sqlite")
    try:
        names = [r[1] for r in conn.execute("PRAGMA table_info(attachments)")]
    finally:
        conn.close()
    assert names == [
        "object_id",
        "token",
        "kind",
        "mime",
        "sha256",
        "size",
        "created_wall_ns",
        "lines",
        "chars",
        "encoding",
        "width",
        "height",
        "filename",
    ]


def test_create_text_metadata(store: AttachmentStore) -> None:
    before = time.time_ns()
    att = store.create_text(b"alpha\nbeta\n", filename="log.txt")
    assert isinstance(att, Attachment)
    assert att.kind is AttachmentKind.TEXT
    assert att.mime == "text/plain"
    assert att.lines == 2
    assert att.chars == 11
    assert att.encoding == "utf-8"
    assert att.size == 11
    assert att.filename == "log.txt"
    assert att.width is None and att.height is None
    assert att.sha256 == hashlib.sha256(b"alpha\nbeta\n").hexdigest()
    assert before <= att.created_wall_ns <= time.time_ns()
    assert store.read_blob(att) == b"alpha\nbeta\n"


@pytest.mark.parametrize(
    ("data", "lines", "chars"),
    [
        (b"", 0, 0),
        (b"\n", 1, 1),
        (b"a", 1, 1),
        (b"a\n", 1, 2),
        (b"a\nb", 2, 3),
        (b"a\nb\n", 2, 4),
        (b"\n\n\n", 3, 3),
        ("ümlaut\nzeile".encode(), 2, 12),
    ],
)
def test_text_line_and_char_counts(store: AttachmentStore, data: bytes, lines: int, chars: int) -> None:
    att = store.create_text(data)
    assert (att.lines, att.chars) == (lines, chars)
    assert att.size == len(data)


def test_text_with_invalid_utf8_uses_replacement(store: AttachmentStore) -> None:
    att = store.create_text(b"ok\n\xff\xfe binary\n")
    assert att.encoding == "utf-8 (with replacement)"
    assert att.lines == 2
    assert att.chars == len(b"ok\n\xff\xfe binary\n".decode("utf-8", errors="replace"))
    assert store.read_blob(att) == b"ok\n\xff\xfe binary\n"  # the blob stays byte exact


def test_text_with_declared_encoding(store: AttachmentStore) -> None:
    att = store.create_text("grüße\n".encode("latin-1"), encoding="latin-1")
    assert att.encoding == "latin-1"
    assert att.chars == 6


def test_create_file_and_from_path(store: AttachmentStore, tmp_path: Path) -> None:
    att = store.create_file(b"\x00\x01\x02", mime="application/octet-stream", filename="x.bin")
    assert att.kind is AttachmentKind.FILE
    assert att.lines is None and att.chars is None and att.width is None

    src = tmp_path / "notes.txt"
    src.write_text("one\ntwo\n")
    text_att = store.create_from_path(src)
    assert text_att.kind is AttachmentKind.TEXT
    assert text_att.filename == "notes.txt"
    assert text_att.lines == 2

    img = tmp_path / "shot.png"
    img.write_bytes(png_bytes(8, 5))
    img_att = store.create_from_path(img)
    assert img_att.kind is AttachmentKind.IMAGE
    assert (img_att.width, img_att.height) == (8, 5)
    assert img_att.mime == "image/png"

    blob = tmp_path / "thing.unknownext"
    blob.write_bytes(b"\x7fELF\x02\x01")
    file_att = store.create_from_path(blob)
    assert file_att.kind is AttachmentKind.FILE
    assert file_att.mime == "application/octet-stream"


def test_list_all_and_get(store: AttachmentStore) -> None:
    a = store.create_text(b"one")
    b = store.create_text(b"two")
    assert [x.object_id for x in store.list_all()] == [a.object_id, b.object_id]
    assert store.get(a.object_id) == a
    assert store.get(b.object_id) == b
    assert store.get(999999) is None


def test_metadata_survives_reopen(tmp_path: Path) -> None:
    codec = TokenCodec(load_or_create_secret(tmp_path / "secret.key"))
    first = AttachmentStore(tmp_path, codec)
    att = first.create_text(b"persisted\n", filename="p.txt")
    first.close()

    second = AttachmentStore(tmp_path, codec)
    try:
        assert second.get(att.object_id) == att
        assert second.resolve(att.token).state is ResolutionState.VALID
        # Ids keep increasing after a reopen (AUTOINCREMENT), so tokens are never reused.
        assert second.create_text(b"next\n").object_id == att.object_id + 1
    finally:
        second.close()


# --------------------------------------------------------- lifecycle, modes, safety


def test_close_is_idempotent(tmp_path: Path) -> None:
    codec = TokenCodec(load_or_create_secret(tmp_path / "secret.key"))
    store = AttachmentStore(tmp_path, codec)
    store.create_text(b"closing twice is fine\n")
    store.close()
    assert store.closed is True
    store.close()  # must not raise sqlite3.ProgrammingError
    store.close()


def test_context_manager_exit_after_a_manual_close(tmp_path: Path) -> None:
    codec = TokenCodec(load_or_create_secret(tmp_path / "secret.key"))
    with AttachmentStore(tmp_path, codec) as store:
        store.create_text(b"early close\n")
        store.close()  # the with block must still exit cleanly
    assert store.closed is True


def test_everything_created_is_private_to_the_user(tmp_path: Path) -> None:
    codec = TokenCodec(load_or_create_secret(tmp_path / "secret.key"))
    store = AttachmentStore(tmp_path, codec)
    try:
        att = store.create_text(b"secret notes\n")
        blob = store.blob_path(att)
        for directory in (store.root, store.blobs, blob.parent):
            assert stat.S_ISDIR(directory.stat().st_mode)
            assert oct(directory.stat().st_mode & 0o777) == "0o700", directory
        db = store.root / "attachments.sqlite"
        for path in (blob, db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            if path.exists():
                assert oct(path.stat().st_mode & 0o777) == "0o600", path
        assert (db.with_name(db.name + "-wal")).exists(), "WAL sidecar expected"
    finally:
        store.close()


def test_a_failed_blob_write_leaves_no_temp_file(
    store: AttachmentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):  # directory fsyncs still work
            raise OSError(28, "No space left on device")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError):
        store.create_text(b"this write fails\n")
    monkeypatch.undo()

    leftovers = [p for p in (tmp_path / "attachments" / "blobs").rglob(".tmp-*")]
    assert leftovers == []
    assert blob_files(tmp_path) == []
    assert store.list_all() == [], "no metadata row for a blob that was never written"


# --------------------------------------------------------------- text metadata

def reference_text_metadata(data: bytes, encoding: str | None) -> tuple[int, int, str]:
    """The straightforward implementation this module's chunked one must match."""
    name = encoding or "utf-8"

    def exists(codec: str) -> bool:
        try:
            return bool(codecs.lookup(codec)._is_text_encoding)
        except LookupError:
            return False

    try:
        text = data.decode(name)
        label = name
    except (UnicodeDecodeError, LookupError):
        text = data.decode(name if exists(name) else "utf-8", errors="replace")
        label = f"{name if exists(name) else 'utf-8'} (with replacement)"
    lines = 0 if not text else text.count("\n") + (0 if text.endswith("\n") else 1)
    return lines, len(text), label


@pytest.mark.parametrize(
    ("data", "encoding"),
    [
        (b"", None),
        (b"\n", None),
        (b"plain ascii without a newline", None),
        (b"two\nlines\n", None),
        ("ümlaut\nzeile".encode(), None),
        (b"ok\n\xff\xfe binary\n", None),
        (b"\xc3", None),  # truncated multi byte sequence
        ("grüße\n".encode("latin-1"), "latin-1"),
        ("grüße\n".encode("latin-1"), None),  # declared nothing, invalid utf-8
        (b"whatever", "definitely-not-a-codec"),
        ("é\n".encode("utf-8-sig"), "utf-8-sig"),
        (b"\xef\xbb\xbfwith bom\n", None),
    ],
)
def test_text_metadata_matches_the_straightforward_implementation(
    data: bytes, encoding: str | None
) -> None:
    assert attachments_module._text_metadata(data, encoding) == reference_text_metadata(
        data, encoding
    )


def test_unknown_declared_encoding_falls_back_to_utf8(store: AttachmentStore) -> None:
    att = store.create_text(b"one\ntwo\n", encoding="definitely-not-a-codec")
    assert att.encoding == "utf-8 (with replacement)"
    assert (att.lines, att.chars) == (2, 8)


def test_text_metadata_across_chunk_boundaries() -> None:
    """Multi byte characters and invalid bytes split by the 1 MiB chunking still count once."""
    chunk = attachments_module._TEXT_CHUNK
    for offset in (-1, 0, 1):
        pad = b"a" * (chunk + offset)
        data = pad + "ä".encode() + b"\n" + pad + b"\xff\n"
        assert attachments_module._text_metadata(data, None) == reference_text_metadata(data, None)


def test_text_metadata_does_not_materialise_the_whole_text() -> None:
    data = (b"x" * 63 + b"\n") * 200_000  # 12.8 MB
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        lines, chars, label = attachments_module._text_metadata(data, None)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (lines, chars, label) == (200_000, len(data), "utf-8")
    assert peak < 4 * 1024 * 1024, f"peaked at {peak} bytes for {len(data)} bytes of input"


# ------------------------------------------------------------------ image parsing


@pytest.mark.parametrize(
    ("maker", "mime", "width", "height"),
    [
        (png_bytes, "image/png", 17, 5),
        (gif_bytes, "image/gif", 7, 13),
        (jpeg_bytes, "image/jpeg", 640, 480),
        (webp_vp8x_bytes, "image/webp", 1920, 1080),
        (webp_vp8_bytes, "image/webp", 300, 200),
        (webp_vp8l_bytes, "image/webp", 64, 32),
    ],
)
def test_image_dimensions(store: AttachmentStore, maker, mime: str, width: int, height: int) -> None:
    data = maker(width, height)
    assert image_dimensions(data) == (width, height)
    att = store.create_image(data, mime=mime, filename="img")
    assert att.kind is AttachmentKind.IMAGE
    assert (att.width, att.height) == (width, height)
    assert att.lines is None and att.chars is None


def test_unknown_image_format_leaves_dimensions_none(store: AttachmentStore) -> None:
    for data in (b"", b"not an image at all", b"\x00" * 64, b"BM\x00\x00", png_bytes(2, 2)[:12]):
        assert image_dimensions(data) == (None, None)
    att = store.create_image(b"\x00" * 64, mime="image/x-unknown")
    assert att.width is None and att.height is None


def test_truncated_jpeg_is_not_guessed(store: AttachmentStore) -> None:
    data = jpeg_bytes(100, 50)
    assert image_dimensions(data[:10]) == (None, None)


def test_jpeg_walk_stops_at_the_end_of_image_marker() -> None:
    """Anything appended behind EOI is not part of the image and must be ignored."""
    sof = b"\x08" + struct.pack(">HH", 480, 640) + b"\x01" + b"\x01\x11\x00"
    trailing_sof0 = b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    data = b"\xff\xd8" + b"\xff\xd9" + trailing_sof0
    assert image_dimensions(data) == (None, None)
    # The same SOF0 *before* EOI is of course still read.
    assert image_dimensions(b"\xff\xd8" + trailing_sof0 + b"\xff\xd9") == (640, 480)


def test_jpeg_padding_does_not_take_forever() -> None:
    """20 MB of 0xFF padding must not cost a per-byte walk over the whole file."""
    data = b"\xff\xd8" + b"\xff" * (20 * 1024 * 1024)
    started = time.perf_counter()
    assert image_dimensions(data) == (None, None)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"jpeg scan took {elapsed:.3f}s"


def test_jpeg_beyond_the_scan_limit_is_given_up_on() -> None:
    """A SOF further in than the scan limit yields no dimensions instead of a stall."""
    sof = b"\x08" + struct.pack(">HH", 7, 9) + b"\x01" + b"\x01\x11\x00"
    filler = b"\x00" * 65_000
    comment = b"\xff\xfe" + struct.pack(">H", len(filler) + 2) + filler
    data = b"\xff\xd8" + comment * 24 + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
    assert len(data) > (1 << 20)
    started = time.perf_counter()
    assert image_dimensions(data) == (None, None)
    assert time.perf_counter() - started < 1.0


# ------------------------------------------------------- blobs, dedup and deletion


def test_blob_layout_and_content_addressing(store: AttachmentStore, tmp_path: Path) -> None:
    payload = b"content addressed\n"
    att = store.create_text(payload)
    sha = hashlib.sha256(payload).hexdigest()
    path = store.blob_path(att)
    assert path == tmp_path / "attachments" / "blobs" / sha[:2] / sha
    assert path.read_bytes() == payload
    assert blob_files(tmp_path) == [path]


def test_identical_content_shares_one_blob(store: AttachmentStore, tmp_path: Path) -> None:
    payload = b"exact duplicate payload\n"
    a = store.create_text(payload, filename="a.txt")
    b = store.create_text(payload, filename="b.txt")
    assert a.object_id != b.object_id
    assert a.token != b.token
    assert a.sha256 == b.sha256
    assert len(blob_files(tmp_path)) == 1
    assert store.read_blob(a) == store.read_blob(b) == payload


def test_delete_keeps_a_shared_blob_until_the_last_reference_goes(
    store: AttachmentStore, tmp_path: Path
) -> None:
    payload = b"shared\n"
    a = store.create_text(payload)
    b = store.create_text(payload)
    store.delete(a.object_id)
    assert store.get(a.object_id) is None
    assert store.resolve(a.token).state is ResolutionState.MISSING
    assert len(blob_files(tmp_path)) == 1, "blob removed while still referenced"
    assert store.read_blob(b) == payload

    store.delete(b.object_id)
    assert blob_files(tmp_path) == []
    assert store.list_all() == []


def test_delete_unknown_id_is_a_noop(store: AttachmentStore) -> None:
    att = store.create_text(b"keep me\n")
    store.delete(987654321)
    assert store.get(att.object_id) == att


def test_blob_path_raises_when_missing(store: AttachmentStore) -> None:
    att = store.create_text(b"gone soon\n")
    store.blob_path(att).unlink()
    with pytest.raises(FileNotFoundError):
        store.blob_path(att)
    with pytest.raises(FileNotFoundError):
        store.read_blob(att)


# --------------------------------------------------------------------- resolution


def test_ac09_document_text_contains_only_an_ordinary_token(store: AttachmentStore) -> None:
    """Spec 39.9: attachments appear as ordinary fixed length alphanumeric tokens."""
    att = store.create_text(b"log output\n")
    document = f"- investigate CAN timeout\n  {att.token}\n"
    assert len(att.token) == TOKEN_LENGTH == 22
    assert set(att.token) <= set(ALPHABET)
    assert "[[" not in document and "attachment" not in document
    found = find_candidates(document)
    assert [c for _, _, c in found] == [att.token]
    start, end, candidate = found[0]
    assert document[start:end] == candidate
    res = store.resolve(candidate)
    assert res.state is ResolutionState.VALID
    assert res.attachment is not None and res.attachment.object_id == att.object_id
    assert res.object_id == att.object_id


def test_ac10_arbitrary_alphanumeric_strings_are_ordinary(store: AttachmentStore) -> None:
    """Spec 39.10: arbitrary pasted alphanumeric strings do not validate."""
    store.create_text(b"something\n")
    rng = random.Random(4242)
    for _ in range(20_000):
        candidate = "".join(rng.choices(ALNUM, k=TOKEN_LENGTH))
        res = store.resolve(candidate)
        assert res.state is ResolutionState.ORDINARY
        assert res.attachment is None
    for junk in ("", "short", "x" * 21, "x" * 23, "a b c", "0" * 22, "z" * 22):
        assert store.resolve(junk).state is ResolutionState.ORDINARY


def test_ac11_tokens_from_another_installation_are_ordinary(
    store: AttachmentStore, tmp_path: Path
) -> None:
    """Spec 39.11: tokens issued by another installation do not validate here."""
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_codec = TokenCodec(load_or_create_secret(other_dir / "secret.key"))
    other = AttachmentStore(other_dir, other_codec)
    try:
        foreign = other.create_text(b"their data\n")
        assert other.resolve(foreign.token).state is ResolutionState.VALID
        res = store.resolve(foreign.token)
        assert res.state is ResolutionState.ORDINARY
        assert res.attachment is None and res.object_id is None
        # Even when the very same token string is present in our own token column, a
        # working secret is authoritative and the string stays ordinary text.
        store._conn.execute(
            "UPDATE attachments SET token = ? WHERE object_id = ?",
            (foreign.token, store.create_text(b"ours\n").object_id),
        )
        store._conn.commit()
        assert store.resolve(foreign.token).state is ResolutionState.ORDINARY
    finally:
        other.close()


def test_ac12_valid_token_with_missing_blob_is_distinguishable(store: AttachmentStore) -> None:
    """Spec 39.12: a valid local token with missing backing data is not ordinary text."""
    att = store.create_text(b"important log\n")
    assert store.resolve(att.token).state is ResolutionState.VALID
    store.blob_path(att).unlink()
    res = store.resolve(att.token)
    assert res.state is ResolutionState.MISSING
    assert res.object_id == att.object_id
    assert res.attachment is not None
    assert "blob file missing" in res.detail


def test_ac12_missing_metadata_row_is_reported(store: AttachmentStore) -> None:
    att = store.create_text(b"row disappears\n")
    store.delete(att.object_id)
    res = store.resolve(att.token)
    assert res.state is ResolutionState.MISSING
    assert res.object_id == att.object_id
    assert res.attachment is None
    assert res.detail == "metadata row missing"


def test_truncated_blob_is_missing(store: AttachmentStore) -> None:
    att = store.create_text(b"0123456789\n")
    store.blob_path(att).write_bytes(b"short")
    res = store.resolve(att.token)
    assert res.state is ResolutionState.MISSING
    assert "size mismatch" in res.detail


def test_corrupt_blob_is_detected_only_when_verifying(tmp_path: Path) -> None:
    codec = TokenCodec(load_or_create_secret(tmp_path / "secret.key"))
    store = AttachmentStore(tmp_path, codec)
    try:
        att = store.create_text(b"0123456789\n")
        store.blob_path(att).write_bytes(b"9876543210\n")  # same length, wrong content
        assert store.resolve(att.token).state is ResolutionState.VALID
        store.verify_sha = True
        res = store.resolve(att.token)
        assert res.state is ResolutionState.MISSING
        assert "sha256 mismatch" in res.detail
    finally:
        store.close()


def test_ac13_sequential_attachments_get_unrelated_tokens(store: AttachmentStore) -> None:
    """Spec 39.13: sequentially created attachments receive visually unrelated tokens."""
    tokens = [store.create_text(f"item {i}\n".encode()).token for i in range(50)]
    assert len(set(tokens)) == 50

    def common_prefix(a: str, b: str) -> int:
        n = 0
        while n < len(a) and a[n] == b[n]:
            n += 1
        return n

    for i, a in enumerate(tokens):
        for b in tokens[i + 1 :]:
            assert common_prefix(a, b) <= 3
            assert common_prefix(a[::-1], b[::-1]) <= 3


def test_resolution_of_a_non_attachment_domain_is_ordinary(store: AttachmentStore) -> None:
    token = store.codec.encode(5, DOMAIN_ATTACHMENT + 1)
    res = store.resolve(token)
    assert res.state is ResolutionState.ORDINARY
    assert "domain" in res.detail


def test_key_fallback_resolves_via_the_token_column(tmp_path: Path) -> None:
    codec = TokenCodec(load_or_create_secret(tmp_path / "secret.key"))
    store = AttachmentStore(tmp_path, codec)
    att = store.create_text(b"survives key loss\n")
    store.close()

    # The secret is gone: no codec, resolution falls back to the stored token column.
    (tmp_path / "secret.key").unlink()
    keyless = AttachmentStore(tmp_path, None)
    try:
        res = keyless.resolve(att.token)
        assert res.state is ResolutionState.VALID
        assert res.detail == KEY_FALLBACK_DETAIL
        assert res.attachment is not None and res.attachment.object_id == att.object_id
        assert keyless.read_blob(res.attachment) == b"survives key loss\n"

        assert keyless.resolve("q" * TOKEN_LENGTH).state is ResolutionState.ORDINARY
        assert keyless.resolve("nope").state is ResolutionState.ORDINARY

        keyless.blob_path(att).unlink()
        broken = keyless.resolve(att.token)
        assert broken.state is ResolutionState.MISSING
        assert KEY_FALLBACK_DETAIL in broken.detail

        with pytest.raises(RuntimeError):
            keyless.create_text(b"cannot mint a token without the secret\n")
    finally:
        keyless.close()


def test_regenerated_secret_makes_old_tokens_ordinary(tmp_path: Path) -> None:
    codec = TokenCodec(load_or_create_secret(tmp_path / "secret.key"))
    store = AttachmentStore(tmp_path, codec)
    try:
        att = store.create_text(b"old\n")
        store._codec = TokenCodec(bytes(range(32, 64)))
        assert store.resolve(att.token).state is ResolutionState.ORDINARY
    finally:
        store.close()


# -------------------------------------------------------------------- performance


def test_resolve_one_megabyte_with_ten_thousand_candidates(store: AttachmentStore) -> None:
    real = [store.create_text(f"payload {i}\n".encode()).token for i in range(20)]
    rng = random.Random(11)
    lines = []
    filler = "some ordinary scratchpad text with numbers 1234567890, words and padding "
    for i in range(10_000):
        token = real[i % len(real)] if i % 500 == 0 else "".join(rng.choices(ALNUM, k=TOKEN_LENGTH))
        lines.append(f"{i:06d} {filler}{token} end\n")
    text = "".join(lines)
    assert len(text) > 1_000_000, len(text)

    started = time.perf_counter()
    candidates = find_candidates(text)
    states = [store.resolve(c).state for _, _, c in candidates]
    elapsed = time.perf_counter() - started

    assert len(candidates) == 10_000
    assert states.count(ResolutionState.VALID) == 20
    assert states.count(ResolutionState.ORDINARY) == 9_980
    assert elapsed < 1.0, f"scan and resolve took {elapsed:.3f}s"
