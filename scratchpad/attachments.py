"""Attachment metadata (sqlite) and content addressed blob storage.

Layout below the data directory::

    attachments/attachments.sqlite            metadata, WAL mode
    attachments/blobs/<sha256[:2]>/<sha256>   immutable blobs, shared by identical content

Blobs are written temp file -> fsync -> rename -> directory fsync, so a crash can never
leave a half written blob under its final name (a failed write removes its temp file).
Identical content is stored once; a blob is removed only when the last row referencing
its digest is deleted.

Everything this module creates is private to the user: directories 0700, files 0600,
matching the convention in :mod:`scratchpad.paths`.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import mimetypes
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from pathlib import Path

from scratchpad.tokens import DOMAIN_ATTACHMENT, MAX_OBJECT_ID, TokenCodec

__all__ = [
    "Attachment",
    "AttachmentKind",
    "AttachmentStore",
    "Resolution",
    "ResolutionState",
    "image_dimensions",
]

log = logging.getLogger(__name__)

#: Reported by :meth:`AttachmentStore.resolve` when the installation secret is gone and
#: resolution had to fall back to the stored ``token`` column.
KEY_FALLBACK_DETAIL = "resolved via stored token; secret key unavailable"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    object_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT UNIQUE,
    kind            TEXT NOT NULL,
    mime            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    size            INTEGER NOT NULL,
    created_wall_ns INTEGER NOT NULL,
    lines           INTEGER,
    chars           INTEGER,
    encoding        TEXT,
    width           INTEGER,
    height          INTEGER,
    filename        TEXT
);
CREATE INDEX IF NOT EXISTS attachments_sha256 ON attachments (sha256);
"""

_COLUMNS = (
    "object_id, token, kind, mime, sha256, size, created_wall_ns, "
    "lines, chars, encoding, width, height, filename"
)


class AttachmentKind(StrEnum):
    """What a stored blob is, for preview purposes."""

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"


@dataclass(frozen=True)
class Attachment:
    """One row of the attachment table."""

    object_id: int
    token: str
    kind: AttachmentKind
    mime: str
    sha256: str
    size: int
    created_wall_ns: int
    lines: int | None
    chars: int | None
    encoding: str | None
    width: int | None
    height: int | None
    filename: str | None


class ResolutionState(Enum):
    """Outcome of looking a token candidate up (spec section 16)."""

    ORDINARY = auto()  # does not authenticate: ordinary text, show nothing
    VALID = auto()  # authenticates and blob + metadata exist
    MISSING = auto()  # authenticates but metadata or blob is missing or corrupt


@dataclass(frozen=True)
class Resolution:
    """Result of :meth:`AttachmentStore.resolve`."""

    state: ResolutionState
    token: str
    object_id: int | None
    attachment: Attachment | None
    detail: str


# ------------------------------------------------------------------ image inspection


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")


#: Start-of-frame markers. 0xC4 (DHT), 0xC8 (JPG extension) and 0xCC (DAC) are not SOFs.
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
#: Markers that stand alone, i.e. are not followed by a length field: TEM, the eight
#: restart markers and SOI. 0xD9 (EOI) is deliberately *not* in here: it ends the image
#: and must stop the walk instead of letting it run on into trailing garbage.
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD9)})

#: A SOF sits in the first few kilobytes of every real JPEG. Walking further costs one
#: Python loop iteration per byte, so a padded or hostile file must not drag us along.
_JPEG_SCAN_LIMIT = 1 << 20


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = min(len(data), _JPEG_SCAN_LIMIT)
    while i < n:
        if data[i] != 0xFF:  # not at a marker: the stream is not one we can walk
            return None
        while i < n and data[i] == 0xFF:  # fill bytes
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1
        if marker in _JPEG_STANDALONE:
            continue
        if marker == 0xD9 or marker == 0xDA:  # EOI or start of scan: no SOF found
            return None
        if i + 2 > n:
            return None
        seg_len = int.from_bytes(data[i : i + 2], "big")
        if seg_len < 2 or i + seg_len > n:
            return None
        if marker in _JPEG_SOF:
            if seg_len < 7:
                return None
            height = int.from_bytes(data[i + 3 : i + 5], "big")
            width = int.from_bytes(data[i + 5 : i + 7], "big")
            return width, height
        i += seg_len
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    body = data[20:]
    if chunk == b"VP8X" and len(body) >= 10:
        width = int.from_bytes(body[4:7], "little") + 1
        height = int.from_bytes(body[7:10], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(body) >= 10 and body[3:6] == b"\x9d\x01\x2a":
        width = int.from_bytes(body[6:8], "little") & 0x3FFF
        height = int.from_bytes(body[8:10], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(body) >= 5 and body[0] == 0x2F:
        bits = int.from_bytes(body[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Return ``(width, height)`` for PNG, JPEG, GIF and WebP, else ``(None, None)``."""
    for parser in (_png_dimensions, _gif_dimensions, _webp_dimensions, _jpeg_dimensions):
        size = parser(data)
        if size is not None:
            return size
    return None, None


def sniff_image_mime(data: bytes) -> str | None:
    """Guess an image mime type from magic bytes, or ``None``."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# --------------------------------------------------------------------- text metadata


#: Decoding step size. Large enough to keep the loop overhead invisible, small enough
#: that a multi-hundred-megabyte log never exists twice in memory.
_TEXT_CHUNK = 1 << 20


def _count_lines(data: bytes) -> int:
    """Lines in ``data``: newlines, plus one for a trailing line without a newline."""
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _scan_text(
    data: bytes, name: str, errors: str, *, count_lines: bool
) -> tuple[int, int] | None:
    """Decode ``data`` in chunks and count characters (and lines, on request).

    Returns ``(chars, lines)``, or ``None`` when ``errors="strict"`` finds a byte the
    codec rejects.  The decoded text is never materialised as a whole.
    """
    decoder = codecs.getincrementaldecoder(name)(errors)
    view = memoryview(data)
    chars = 0
    lines = 0
    last = ""
    try:
        for start in range(0, len(data), _TEXT_CHUNK):
            piece = decoder.decode(view[start : start + _TEXT_CHUNK])
            chars += len(piece)
            if count_lines and piece:
                lines += piece.count("\n")
                last = piece[-1]
        piece = decoder.decode(b"", True)
    except UnicodeDecodeError:
        return None
    chars += len(piece)
    if count_lines:
        if piece:
            lines += piece.count("\n")
            last = piece[-1]
        if chars and last != "\n":
            lines += 1
    return chars, lines


def _text_metadata(data: bytes, encoding: str | None) -> tuple[int, int, str]:
    """Return ``(lines, chars, encoding_label)`` for ``data``.

    Counting never builds the decoded string: characters come from an incremental
    decoder fed in :data:`_TEXT_CHUNK` slices and lines from the raw bytes whenever the
    codec encodes ``"\\n"`` as a bare ``0x0A`` (every ASCII compatible codec does).
    """
    name = encoding or "utf-8"
    if _codec_exists(name):
        ascii_newlines = _newline_is_a_byte(name)
        counted = _scan_text(data, name, "strict", count_lines=not ascii_newlines)
        if counted is not None:
            label = name
        else:
            counted = _scan_text(data, name, "replace", count_lines=not ascii_newlines)
            label = f"{name} (with replacement)"
    else:
        # An unknown codec name makes even well formed input fall back to utf-8.
        ascii_newlines = True
        counted = _scan_text(data, "utf-8", "replace", count_lines=False)
        label = "utf-8 (with replacement)"
    assert counted is not None  # pragma: no cover - a "replace" decoder never rejects
    chars, lines = counted
    if ascii_newlines:
        lines = _count_lines(data)
    return lines, chars, label


def _codec_exists(name: str) -> bool:
    """True when ``name`` names a usable *text* codec.

    ``b"".decode(name)`` is not a usable probe: CPython short circuits empty input and
    returns ``""`` for names that do not exist at all.
    """
    try:
        info = codecs.lookup(name)
    except LookupError:
        return False
    return bool(getattr(info, "_is_text_encoding", True))


def _newline_is_a_byte(name: str) -> bool:
    """True when the codec writes ``"\\n"`` as a single 0x0A byte (so bytes can be counted)."""
    try:
        return "\n".encode(name) == b"\n"
    except (LookupError, UnicodeError):  # pragma: no cover - exotic codecs only
        return False


# ------------------------------------------------------------------------ the store


#: Everything we create is private to the user (same convention as scratchpad.paths).
DIR_MODE = 0o700
FILE_MODE = 0o600


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_private_dir(directory: Path) -> None:
    """Create ``directory`` (and parents) and make sure it is mode 0700."""
    directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    try:
        os.chmod(directory, DIR_MODE)
    except OSError:  # pragma: no cover - not the owner
        log.warning("cannot set mode 0700 on %s", directory)


def _make_private(path: Path) -> None:
    """Best effort ``chmod 0600`` for a file that may or may not exist."""
    try:
        os.chmod(path, FILE_MODE)
    except FileNotFoundError:
        pass
    except OSError:  # pragma: no cover - not the owner
        log.warning("cannot set mode 0600 on %s", path)


class AttachmentStore:
    """Metadata database plus content addressed blob directory.

    ``codec`` may be ``None`` (or :meth:`resolve` may be told the secret is unavailable),
    in which case resolution falls back to the stored ``token`` column and creation is
    refused.
    """

    def __init__(
        self,
        data_dir: Path,
        codec: TokenCodec | None,
        *,
        verify_sha: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "attachments"
        self.blobs = self.root / "blobs"
        _ensure_private_dir(self.root)
        _ensure_private_dir(self.blobs)
        self._codec = codec
        #: When true, :meth:`resolve` re-hashes the blob. Off by default: it is O(size).
        self.verify_sha = verify_sha
        self._closed = False
        self.db_path = self.root / "attachments.sqlite"
        # sqlite creates the database and its -wal/-shm siblings itself, so narrow the
        # umask around the connection and chmod afterwards for an inherited database.
        old_mask = os.umask(0o177)
        try:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        finally:
            os.umask(old_mask)
        self._make_db_private()

    def _make_db_private(self) -> None:
        """Mode 0600 for the database and the WAL sidecars sqlite created."""
        for path in (
            self.db_path,
            self.db_path.with_name(self.db_path.name + "-wal"),
            self.db_path.with_name(self.db_path.name + "-shm"),
        ):
            _make_private(path)

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        """Commit and close the database connection. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has run."""
        return self._closed

    def __enter__(self) -> AttachmentStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def codec(self) -> TokenCodec | None:
        """The codec in use, or ``None`` when the installation secret is unavailable."""
        return self._codec

    # -- blobs -------------------------------------------------------------------

    def _blob_path(self, sha256: str) -> Path:
        return self.blobs / sha256[:2] / sha256

    def _write_blob(self, data: bytes, sha256: str) -> None:
        path = self._blob_path(sha256)
        try:
            if path.stat().st_size == len(data):
                return  # identical content already stored: share the blob
            log.warning("blob %s has unexpected size, rewriting it", sha256)
        except FileNotFoundError:
            pass
        shard = path.parent
        created_shard = not shard.exists()
        _ensure_private_dir(shard)
        if created_shard:
            _fsync_dir(self.blobs)
        tmp = shard / f".tmp-{os.getpid()}-{secrets.token_hex(8)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        try:
            try:
                written = 0
                view = memoryview(data)
                while written < len(data):
                    written += os.write(fd, view[written:])
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except BaseException:
            # A full disk, an I/O error or a KeyboardInterrupt must not leave a
            # half written temp file behind for the blob GC to trip over.
            try:
                os.unlink(tmp)
            except OSError:  # pragma: no cover - already gone
                pass
            raise
        _fsync_dir(shard)

    def blob_path(self, attachment: Attachment) -> Path:
        """Path of the blob backing ``attachment``; raises if it is not present."""
        path = self._blob_path(attachment.sha256)
        if not path.exists():
            raise FileNotFoundError(f"blob for attachment {attachment.object_id} is missing: {path}")
        return path

    def read_blob(self, attachment: Attachment) -> bytes:
        """Return the content of ``attachment``."""
        return self.blob_path(attachment).read_bytes()

    # -- creation ----------------------------------------------------------------

    def _insert(
        self,
        *,
        kind: AttachmentKind,
        mime: str,
        data: bytes,
        filename: str | None,
        lines: int | None = None,
        chars: int | None = None,
        encoding: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> Attachment:
        if self._codec is None:
            raise RuntimeError("cannot create attachments: installation secret unavailable")
        sha256 = hashlib.sha256(data).hexdigest()
        self._write_blob(data, sha256)
        created_wall_ns = time.time_ns()
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO attachments (token, kind, mime, sha256, size, created_wall_ns,"
                " lines, chars, encoding, width, height, filename)"
                " VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(kind),
                    mime,
                    sha256,
                    len(data),
                    created_wall_ns,
                    lines,
                    chars,
                    encoding,
                    width,
                    height,
                    filename,
                ),
            )
            object_id = int(cur.lastrowid)
            if object_id > MAX_OBJECT_ID:
                raise OverflowError(f"object_id {object_id} exceeds the 56 bit token field")
            token = self._codec.encode(object_id, DOMAIN_ATTACHMENT)
            self._conn.execute(
                "UPDATE attachments SET token = ? WHERE object_id = ?", (token, object_id)
            )
        return Attachment(
            object_id=object_id,
            token=token,
            kind=kind,
            mime=mime,
            sha256=sha256,
            size=len(data),
            created_wall_ns=created_wall_ns,
            lines=lines,
            chars=chars,
            encoding=encoding,
            width=width,
            height=height,
            filename=filename,
        )

    def create_text(
        self,
        data: bytes,
        *,
        mime: str = "text/plain",
        filename: str | None = None,
        encoding: str | None = None,
    ) -> Attachment:
        """Store text. Computes line and character counts and the effective encoding."""
        lines, chars, label = _text_metadata(data, encoding)
        return self._insert(
            kind=AttachmentKind.TEXT,
            mime=mime,
            data=data,
            filename=filename,
            lines=lines,
            chars=chars,
            encoding=label,
        )

    def create_image(
        self, data: bytes, *, mime: str, filename: str | None = None
    ) -> Attachment:
        """Store an image. Dimensions are parsed where the format is understood."""
        width, height = image_dimensions(data)
        return self._insert(
            kind=AttachmentKind.IMAGE,
            mime=mime,
            data=data,
            filename=filename,
            width=width,
            height=height,
        )

    def create_file(self, data: bytes, *, mime: str, filename: str | None) -> Attachment:
        """Store an arbitrary file."""
        return self._insert(
            kind=AttachmentKind.FILE, mime=mime, data=data, filename=filename
        )

    def create_from_path(self, path: Path) -> Attachment:
        """Store the file at ``path``, choosing the kind from its guessed mime type."""
        path = Path(path)
        data = path.read_bytes()
        mime, _ = mimetypes.guess_type(path.name)
        if mime is None:
            mime = sniff_image_mime(data)
        if mime is None:
            mime = "application/octet-stream"
        if mime.startswith("image/"):
            return self.create_image(data, mime=mime, filename=path.name)
        if mime.startswith("text/"):
            return self.create_text(data, mime=mime, filename=path.name)
        return self.create_file(data, mime=mime, filename=path.name)

    # -- queries -----------------------------------------------------------------

    @staticmethod
    def _row_to_attachment(row: sqlite3.Row) -> Attachment:
        return Attachment(
            object_id=row["object_id"],
            token=row["token"],
            kind=AttachmentKind(row["kind"]),
            mime=row["mime"],
            sha256=row["sha256"],
            size=row["size"],
            created_wall_ns=row["created_wall_ns"],
            lines=row["lines"],
            chars=row["chars"],
            encoding=row["encoding"],
            width=row["width"],
            height=row["height"],
            filename=row["filename"],
        )

    def get(self, object_id: int) -> Attachment | None:
        """Return the attachment with ``object_id``, or ``None``."""
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM attachments WHERE object_id = ?", (object_id,)
        ).fetchone()
        return None if row is None else self._row_to_attachment(row)

    def get_by_token(self, token: str) -> Attachment | None:
        """Return the attachment whose stored token column equals ``token``, or ``None``."""
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM attachments WHERE token = ?", (token,)
        ).fetchone()
        return None if row is None else self._row_to_attachment(row)

    def list_all(self) -> list[Attachment]:
        """Every attachment, ordered by object id."""
        rows = self._conn.execute(f"SELECT {_COLUMNS} FROM attachments ORDER BY object_id")
        return [self._row_to_attachment(row) for row in rows]

    # -- resolution --------------------------------------------------------------

    def _check_blob(self, attachment: Attachment, candidate: str, detail: str) -> Resolution:
        path = self._blob_path(attachment.sha256)
        problem = ""
        try:
            size = path.stat().st_size
        except OSError as exc:
            problem = f"blob file missing ({exc.__class__.__name__})"
        else:
            if size != attachment.size:
                problem = f"blob size mismatch: expected {attachment.size}, found {size}"
            elif self.verify_sha:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest != attachment.sha256:
                    problem = "blob sha256 mismatch"
        parts = [p for p in (problem, detail) if p]
        if problem:
            return Resolution(
                ResolutionState.MISSING,
                candidate,
                attachment.object_id,
                attachment,
                "; ".join(parts),
            )
        return Resolution(
            ResolutionState.VALID, candidate, attachment.object_id, attachment, detail
        )

    def resolve(self, candidate: str) -> Resolution:
        """Classify a token candidate as ORDINARY, VALID or MISSING.

        With a working secret the cryptographic decision is authoritative: a candidate
        that does not authenticate is ordinary text, even if the literal string happens
        to sit in the ``token`` column. That is deliberate. The column is only consulted
        when the secret itself is unavailable (``codec is None``); consulting it otherwise
        would make tokens minted by *another* installation resolve here as soon as a
        collision or an imported database supplied the same string, which is exactly the
        property spec section 17 requires us not to have.
        """
        if self._codec is not None:
            decoded = self._codec.decode(candidate)
            if decoded is None:
                return Resolution(ResolutionState.ORDINARY, candidate, None, None, "")
            object_id, domain = decoded
            if domain != DOMAIN_ATTACHMENT:
                # Authenticates, but belongs to another (future) object class. Nothing to
                # preview and nothing is broken, so it is not an attachment reference.
                return Resolution(
                    ResolutionState.ORDINARY,
                    candidate,
                    object_id,
                    None,
                    f"authenticates in domain {domain}, not an attachment",
                )
            attachment = self.get(object_id)
            if attachment is None:
                return Resolution(
                    ResolutionState.MISSING, candidate, object_id, None, "metadata row missing"
                )
            return self._check_blob(attachment, candidate, "")

        # Secret key unavailable: fall back to the stored token column (spec question 17).
        if len(candidate) != TokenCodec.TOKEN_LENGTH:
            return Resolution(ResolutionState.ORDINARY, candidate, None, None, "")
        attachment = self.get_by_token(candidate)
        if attachment is None:
            return Resolution(ResolutionState.ORDINARY, candidate, None, None, "")
        return self._check_blob(attachment, candidate, KEY_FALLBACK_DETAIL)

    # -- deletion ----------------------------------------------------------------

    def delete(self, object_id: int) -> None:
        """Delete one attachment row, and its blob if no other row shares the digest."""
        row = self._conn.execute(
            "SELECT sha256 FROM attachments WHERE object_id = ?", (object_id,)
        ).fetchone()
        if row is None:
            log.debug("delete: attachment %d does not exist", object_id)
            return
        sha256 = row["sha256"]
        with self._conn:
            self._conn.execute("DELETE FROM attachments WHERE object_id = ?", (object_id,))
        shared = self._conn.execute(
            "SELECT 1 FROM attachments WHERE sha256 = ? LIMIT 1", (sha256,)
        ).fetchone()
        if shared is not None:
            return
        path = self._blob_path(sha256)
        try:
            path.unlink()
        except FileNotFoundError:
            log.warning("blob %s was already gone when deleting attachment %d", sha256, object_id)
            return
        _fsync_dir(path.parent)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        else:
            _fsync_dir(self.blobs)
