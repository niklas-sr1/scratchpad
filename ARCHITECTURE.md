# Persistent Scratchpad: Implementation Architecture and Interface Contract

This document is the binding contract between the modules of the application.
The product requirements live in `Design Specification.md`; read it first.
Where this document makes a decision, follow it. Where it is silent, follow the spec.

## 1. Stack and environment

| Item | Decision |
|---|---|
| Language | Python 3.14 (system `/usr/bin/python3`), type hints everywhere |
| GUI | GTK 4.22 + libadwaita 1.9 via PyGObject (`gi`). No GtkSourceView (not installed). |
| Crypto | `cryptography` (AES-128) and `hashlib.blake2b` (keyed MAC). Both installed system-wide. |
| Storage | Custom append-only binary event log + sqlite3 (stdlib) for attachment metadata |
| Tests | `.venv/bin/python -m pytest` (venv has system site packages, pytest, hypothesis) |
| Desktop | Manjaro, Wayland, COSMIC desktop. Screenshot via xdg-desktop-portal (`org.freedesktop.portal.Screenshot`), `cosmic-screenshot` CLI as fallback. |
| Dependencies | stdlib + PyGObject + cryptography only. No other third-party packages. |

Run tests from the project root: `cd /home/niklas/Code/scratchpad && .venv/bin/python -m pytest -q`.
Core modules (`scratchpad/core`, `scratchpad/tokens.py`, `scratchpad/attachments.py`,
`scratchpad/redaction.py`, `scratchpad/ipc.py`, `scratchpad/cli.py`) must import and be testable
WITHOUT importing `gi`. Only `scratchpad/ui/` may import `gi`.

## 2. Repository layout and file ownership

```
scratchpad/                      Python package
  __init__.py                    version string
  __main__.py                    -> cli.main()
  paths.py                       data/config/runtime directory resolution   [Agent A]
  config.py                      config.toml loading with defaults          [Agent A]
  core/
    __init__.py
    varint.py                    LEB128 unsigned/zigzag varints             [Agent A]
    clock.py                     monotonic + wall clock anchoring           [Agent A]
    ops.py                       Op / Event dataclasses, apply_op           [Agent A]
    eventlog.py                  binary log format, reader, writer          [Agent A]
    checkpoint.py                checkpoint files                           [Agent A]
    history.py                   sessions, index, reconstruction            [Agent A]
    store.py                     ScratchpadStore facade                     [Agent A]
    recovery.py                  recover_pending_rewrite() hook             [Agent A stub, Agent E fills]
  tokens.py                      Base62, keyed PRP, MAC, TokenCodec, key mgmt [Agent B]
  attachments.py                 AttachmentStore, Resolution                 [Agent B]
  ipc.py                         protocol, IpcServer (GLib-free), IpcClient  [Agent C]
  cli.py                         scratchpad / scratchpad-attach / -insert    [Agent C]
  redaction.py                   history rewrite, redact, purge              [Agent E]
  gc.py                          attachment reference scan + blob GC         [Agent E]
  textdiff.py                    minimal diff between two texts (pure)       [Agent E]
  ui/
    __init__.py
    app.py                       Adw.Application, single instance, IPC wiring [Agent D1]
    window.py                    main window, panes, menus, history mode      [Agent D1]
    editor.py                    TextView + mutation interception             [Agent D1]
    commands.py                  command registry (menus + palette share it)  [Agent D1]
    palette.py                   command palette                              [Agent D1]
    capture.py                   clipboard, paste guardrail, screenshot       [Agent D1]
    dialogs.py                   confirmations, large-paste dialog, redaction [Agent D1]
    preview.py                   PreviewPane widget                           [Agent D2]
    timeline.py                  TimelineWidget                               [Agent D2]
    textview_extras.py           LineNumberedTextView with search             [Agent D2]
    diffview.py                  DiffView widget                              [Agent D2]
tests/                           pytest; each agent owns tests for its modules
  test_<module>.py
data/
  dev.scratchpad.Scratchpad.desktop
pyproject.toml                   (owned by orchestrator; do not edit)
```

Do not edit files owned by another agent. If you need something from another module that the
contract does not provide, implement a small local helper in your own module and note it in your
final report.

## 3. Data directory layout

`paths.data_dir()` returns `$SCRATCHPAD_DATA_DIR` if set, else `$XDG_DATA_HOME/scratchpad`
(default `~/.local/share/scratchpad`). Config: `$XDG_CONFIG_HOME/scratchpad/config.toml`.
Runtime: `$XDG_RUNTIME_DIR/scratchpad/` (fallback `/tmp/scratchpad-<uid>/`).

```
<data_dir>/
  lock                     flock'd by the running GUI instance (single instance enforced)
  secret.key               32 random bytes, mode 0600, created on first use
  history/
    events.log             the append-only event log (single file)
    checkpoints/
      <seq:016d>.ckpt      full document text at event seq N (after applying event N)
    index.cache            optional rebuildable time index (may be absent)
  attachments/
    attachments.sqlite     metadata
    blobs/<sha256[:2]>/<sha256>   immutable content-addressed blobs
  current.txt              latest durable full text (written at checkpoint and clean shutdown)
```

Multiple simultaneous GUI instances are prohibited (flock). CLI tools never open the event log
directly while the GUI runs; they talk to the GUI over IPC. If the GUI is not running, the CLI
starts it (see section 8).

## 4. Document model and operations (Agent A, `core/ops.py`)

Positions are Unicode code point offsets (matches `Gtk.TextBuffer` character offsets and Python
`str` indexing). Text is stored as UTF-8 in the log.

```python
class OpKind(IntEnum): INSERT = 1; DELETE = 2; REPLACE = 3

@dataclass(frozen=True, slots=True)
class Op:
    kind: OpKind
    pos: int
    text: str = ""          # INSERT: inserted text. REPLACE: new text.
    old_text: str = ""      # DELETE: deleted text (we store the text, not only the length). REPLACE: old text.

def apply_op(doc: str, op: Op) -> str      # raises OpError if op does not fit doc (pos/old_text mismatch)
def invert_op(op: Op) -> Op                # for backward stepping
```

Every mutation of the editor buffer becomes exactly one `Op`. Undo/redo produce ordinary ops.
"Clear scratchpad" is `DELETE(0, whole_text)`. "Restore historical state" is `REPLACE(0, cur, hist)`.

## 5. Event log (Agent A, `core/eventlog.py`)

Event kinds:

```python
class EventKind(IntEnum):
    SESSION_START = 1   # payload: session_id (u64 random), wall_ns (u64), mono_ns (u64), app_version (str)
    SESSION_STOP  = 2   # clean shutdown
    HEARTBEAT     = 3   # emitted every config.heartbeat_seconds while running (default 30)
    WALL_ANCHOR   = 4   # payload: wall_ns; emitted with every heartbeat and whenever wall-mono drift > 1s
    INSERT        = 5   # payload: pos (varint), text (len-prefixed utf8)
    DELETE        = 6   # payload: pos, old_text
    REPLACE       = 7   # payload: pos, old_text, text
```

Record framing (all integers LEB128 varints unless stated):

```
[len]        total length of the remainder of this record (kind..payload), varint
[kind]       varint
[mono_delta] varint, nanoseconds since the previous record in the file (0 for the first record;
             for SESSION_START it is 0 and absolute times are in the payload)
[payload]    kind-specific
[crc32]      4 bytes little-endian, CRC32 of (kind..payload)
```

File header: magic `SCRPLOG1` (8 bytes), format version u16 LE, reserved u16 LE, then records.
A reader stops at the first record whose length is truncated or whose CRC mismatches and reports
`tail_ok=False` plus the byte offset of the good tail; the writer then truncates the file to that
offset before appending (torn-write recovery). Never rewrite good records in place.

Ordinary typing of one ASCII character must cost at most 8 bytes total on disk
(len 1 + kind 1 + delta ~2 + pos ~1-3 + textlen 1 + text 1 + crc 4 is 11; if that cannot be met,
reduce the CRC to a 1-byte CRC8 for INSERT/DELETE records only or move the CRC to
per-group framing. Agent A decides and documents the final size; target is under 12 bytes.)

Every record has a derived wall-clock timestamp. Within a session,
`wall = anchor_wall + (record_mono - anchor_mono)` using the most recent SESSION_START or WALL_ANCHOR.
For timeline purposes the derived wall time sequence is clamped to be non-decreasing.

Each event gets a sequence number `seq` (0-based position in the log). `Event` is:

```python
@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    kind: EventKind
    mono_ns: int          # absolute monotonic within the session
    wall_ns: int          # derived wall-clock, non-decreasing
    session_id: int
    op: Op | None         # for INSERT/DELETE/REPLACE
    offset: int           # byte offset of the record in the file
```

Writer: `EventLogWriter(path)` with `append(kind, mono_delta, payload) -> offset`, buffered in
memory and written to the OS on every append (`os.write`), with `fsync()` performed by a timer
(`config.flush_interval_ms`, default 500) and on `close()`. Provide `flush(fsync: bool)`.
Reader: `EventLogReader(path)` with `iter_events(start_offset=0)`, `scan() -> ScanResult(tail_ok, good_length, count, last_event)`.

## 6. Checkpoints and history (Agent A, `core/checkpoint.py`, `core/history.py`)

Checkpoint file `<seq:016d>.ckpt`: magic `SCRPCKP1`, u16 version, u64 seq, u64 wall_ns,
u32 crc32 of the uncompressed text, then zlib-compressed UTF-8 text. Written to a temp file,
fsynced, renamed. Written every `config.checkpoint_every_events` (default 2000) events and at clean
shutdown. Checkpoints are a cache: deleting them all must not lose anything.

```python
@dataclass
class Session: session_id: int; start_wall_ns: int; end_wall_ns: int; clean_stop: bool; first_seq: int; last_seq: int

class History:
    def sessions(self) -> list[Session]
    def time_range(self) -> tuple[int, int]                  # first and last known wall_ns
    def reconstruct_at(self, wall_ns: int) -> tuple[str, int]  # text and seq of the last applied event (<= wall_ns)
    def reconstruct_seq(self, seq: int) -> str
    def events(self, start_seq: int, end_seq: int) -> Iterator[Event]
    def activity(self, start_wall_ns: int, end_wall_ns: int, buckets: int) -> list[int]  # edit counts per bucket
    def event_at_or_before(self, wall_ns: int) -> Event | None
    def event_after(self, wall_ns: int) -> Event | None
```

Reconstruction: newest checkpoint with seq <= target, then replay. Keep an in-memory index of
(wall_ns, seq, offset) sampled at least every 256 events, built by scanning the log at open
(persist to `index.cache` if you like; it must be validated against log length and rebuilt if stale).

## 7. Store facade (Agent A, `core/store.py`)

```python
class ScratchpadStore:
    @classmethod
    def open(cls, data_dir: Path, config: Config, *, app_version: str) -> "ScratchpadStore"
        # creates layout, runs recovery.recover_pending_rewrite(data_dir), truncates torn tail,
        # loads current text (fast path: current.txt if its recorded seq matches the log, else replay),
        # appends SESSION_START. Takes the flock; raises StoreLockedError if another instance holds it.
    text: str                                   # current document (read-only property)
    def apply(self, op: Op) -> Event            # validates against text, appends event, updates text
    def heartbeat(self) -> None                 # append HEARTBEAT (+WALL_ANCHOR); UI calls on a timer
    def flush(self, fsync: bool = True) -> None
    def checkpoint(self) -> None                # force a checkpoint + current.txt
    def close(self) -> None                     # SESSION_STOP, checkpoint, fsync, release lock
    history: History
    def replace_history(self, new_events_path: Path, new_checkpoints_dir: Path | None) -> None
        # crash-safe swap used by redaction (Agent E defines the protocol in recovery.py; the store
        # closes and reopens its reader/writer around the swap). Current text is re-derived.
```

The store must never block the UI thread for more than a few ms on `apply()`.

## 8. IPC (Agent C, `ipc.py`) and CLI (`cli.py`)

Unix stream socket at `<runtime_dir>/ipc.sock`. One request per connection.
Request: one JSON object terminated by `\n`. If it contains `"payload_size": N`, exactly N raw
bytes follow the newline (binary payload, no base64). Response: one JSON line
`{"ok": true, ...}` or `{"ok": false, "error": "..."}` then the server closes.

Commands (request fields besides `cmd`):

| cmd | fields | effect |
|---|---|---|
| `ping` | | `{"ok":true,"version":...}` |
| `toggle` | `activation_token?` | show if hidden else hide |
| `show` / `hide` | `activation_token?` | |
| `insert` | `text` or payload (utf-8), `where`: `"cursor"` (default) or `"end"` | insert text at cursor; if not ending with newline and `where=="end"`, prefix a newline if the doc does not end with one |
| `attach` | `kind`: `text`/`image`/`file`, `mime?`, `filename?`, payload = content | create attachment, insert its token at cursor followed by nothing else; response includes `token`, `object_id` |
| `attach_path` | `path` | attach an existing file by path (kind inferred from mime) |
| `screenshot` | | run the screenshot flow (portal) |
| `paste_clipboard` | `activation_token?` | show the window and paste the clipboard (fast capture) |
| `status` | | `{"ok":true,"visible":bool,"chars":int,"events":int}` |

`activation_token` is the value of `XDG_ACTIVATION_TOKEN` in the CLI process environment when set;
the GUI passes it to `Gtk.Window.set_startup_id()` before `present()` so Wayland focus works.

`IpcServer` must not depend on GLib: it exposes `fileno()` for `GLib.io_add_watch` and
`handle_ready()` which accepts one connection, reads the request and payload, dispatches to a
`handler(request: dict, payload: bytes | None) -> dict` callable supplied by the UI, and writes
the response. Requests are handled on the GTK main thread. `IpcClient.request(dict, payload=None, timeout=...)`.

CLI entry points (`pyproject.toml` scripts, also `python -m scratchpad`):

- `scratchpad [toggle|show|hide|status|screenshot|paste-clipboard]` (default: `toggle` if running, else start GUI)
- `scratchpad-attach [--mime M] [--name N] [FILE]` reads FILE or stdin until EOF, sends `attach`
- `scratchpad-insert [--end] [TEXT...]` reads args or stdin, sends `insert`
- `scratchpad gui` starts the GUI in the foreground (`scratchpad.ui.app:main`)
- `scratchpad install-shortcut [--key "Super+grave"]` writes the COSMIC custom shortcut (see section 12)

If the socket is not reachable, the CLI (except `hide`/`status`) spawns `scratchpad gui`
detached (`start_new_session=True`) and retries the request for up to 8 seconds.

## 9. Tokens (Agent B, `tokens.py`)

Token: exactly 22 characters from `0-9A-Za-z`, fixed width, representing a 128-bit value.

```
payload (16 bytes) = object_id (56 bits, big-endian) || domain (8 bits) || tag (64 bits)
tag                = blake2b(key=mac_key, digest_size=8, data = b"scratchpad-token-v1" || domain_byte || object_id_7bytes)
prp                = AES-128-ECB single-block encrypt of payload with prp_key   (a keyed pseudorandom permutation on 128 bits)
token              = base62_fixed22(int.from_bytes(prp, "big"))
```

Base62 alphabet `0-9A-Za-z` in that order, most significant digit first, left-padded with `0` to
22 characters. Decoding rejects: wrong length, chars outside the alphabet, and values >= 2^128
(62^22 > 2^128, so such strings exist and are never tokens). Decoding inverts the AES block,
recomputes the tag, and compares in constant time. `DOMAIN_ATTACHMENT = 1`.

Keys: `secret.key` holds 32 random bytes (mode 0600, created atomically). Derive
`prp_key = blake2b(key=secret, digest_size=16, data=b"prp")`, `mac_key = blake2b(key=secret, digest_size=32, data=b"mac")`.

```python
class TokenCodec:
    TOKEN_LENGTH = 22
    def __init__(self, secret: bytes)
    def encode(self, object_id: int, domain: int) -> str
    def decode(self, token: str) -> tuple[int, int] | None        # (object_id, domain) or None if not ours
    @staticmethod
    def find_candidates(text: str) -> list[tuple[int, int, str]]  # (start, end, candidate) maximal [0-9A-Za-z] runs of exactly 22 chars
    @staticmethod
    def candidate_at(text: str, pos: int) -> tuple[int, int, str] | None  # the candidate run containing pos (pos may equal end)
def load_or_create_secret(path: Path) -> bytes
```

Tests must show: sequential ids give tokens with no shared prefix/suffix beyond chance, other
keys do not validate, random 22-char strings do not validate, round-trip for ids up to 2^56-1.

## 10. Attachments (Agent B, `attachments.py`)

```python
class AttachmentKind(StrEnum): TEXT = "text"; IMAGE = "image"; FILE = "file"

@dataclass(frozen=True)
class Attachment:
    object_id: int; token: str; kind: AttachmentKind; mime: str; sha256: str; size: int
    created_wall_ns: int; lines: int | None; chars: int | None; encoding: str | None
    width: int | None; height: int | None; filename: str | None

class ResolutionState(Enum): ORDINARY = auto(); VALID = auto(); MISSING = auto()
    # ORDINARY: does not authenticate. VALID: authenticates and blob + metadata exist.
    # MISSING: authenticates (or matches the stored token column) but metadata or blob is missing/corrupt.

@dataclass(frozen=True)
class Resolution: state: ResolutionState; token: str; object_id: int | None; attachment: Attachment | None; detail: str

class AttachmentStore:
    def __init__(self, data_dir: Path, codec: TokenCodec)
    def create_text(self, data: bytes, *, mime="text/plain", filename=None, encoding=None) -> Attachment   # computes lines/chars; decodes utf-8 with replacement for counting
    def create_image(self, data: bytes, *, mime: str, filename=None) -> Attachment                            # width/height via stdlib parsing of PNG/JPEG/GIF/WebP headers where possible
    def create_file(self, data: bytes, *, mime: str, filename: str | None) -> Attachment
    def create_from_path(self, path: Path) -> Attachment                                                     # kind from mime guess
    def get(self, object_id: int) -> Attachment | None
    def resolve(self, candidate: str) -> Resolution
    def blob_path(self, attachment: Attachment) -> Path                                                      # raises if missing
    def read_blob(self, attachment: Attachment) -> bytes
    def list_all(self) -> list[Attachment]
    def delete(self, object_id: int) -> None                                                                 # removes row; removes blob if no other row shares the sha
    def close(self) -> None
```

sqlite table `attachments(object_id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE, kind, mime, sha256, size, created_wall_ns, lines, chars, encoding, width, height, filename)`.
Blobs are written to a temp file, fsynced, renamed into `blobs/<sha[:2]>/<sha>`; identical content
shares one blob. The `token` column is a fallback for a lost secret (resolution then reports
`VALID` with `detail` noting the key fallback). sqlite must be opened with WAL and be safe for the
GUI process; the CLI never opens it while the GUI runs.

## 11. Redaction, purge, GC (Agent E, `redaction.py`, `gc.py`, `textdiff.py`)

Rewrite protocol (crash-safe):

1. Write `history/events.log.rewrite` completely, fsync.
2. Write `history/checkpoints.rewrite/` (may be empty), fsync dir.
3. Write journal `history/REWRITE_PENDING` containing a JSON line `{"phase": "swap"}`, fsync.
4. Rename `events.log` -> `events.log.old`, `events.log.rewrite` -> `events.log`, `checkpoints` -> `checkpoints.old`, `checkpoints.rewrite` -> `checkpoints`, delete `current.txt`, `index.cache`.
5. Remove `events.log.old`, `checkpoints.old`, then `REWRITE_PENDING`.

`recovery.recover_pending_rewrite(data_dir)`: if `REWRITE_PENDING` exists, finish step 4/5
idempotently; if `events.log.rewrite` exists without the journal, delete it (aborted rewrite).

```python
def redact_text(store, text: str, *, within: tuple[int, int] | None, dry_run=False) -> RedactionReport
    # Removes every occurrence of `text` from every historical state (within the wall_ns window if given,
    # window expanded to cover contiguous states containing the text). Algorithm: replay states,
    # transform each state, emit minimal diff ops (textdiff) between consecutive transformed states with the
    # original timestamps; drop no-op events; keep SESSION_*/HEARTBEAT/WALL_ANCHOR unchanged.
def purge_attachment(store, attachments, object_id: int, *, dry_run=False) -> RedactionReport
    # redact_text(token, within=None) then attachments.delete(object_id)
def collect_garbage(store, attachments, codec, *, dry_run=False) -> GcReport
    # Full replay scanning the window around each edit for token candidates (a token can be formed by
    # several edits, so scan [pos-22, pos+len(text)+22] of the resulting state), plus current text.
    # Any object_id never referenced is deleted. Report what was deleted.
```

`RedactionReport`: `events_before`, `events_after`, `occurrences_removed`, `states_changed`.
The UI shows the report and the exact wording "Remove permanently from scratchpad history" (never
"secure erase").

## 12. UI (Agent D1, D2)

Application id `dev.scratchpad.Scratchpad`. Default window: 900x600, editor on the left (monospace,
wrap none, tabs 4 spaces, `Gtk.TextBuffer` with `enable_undo=True`), right sidebar with two stacked
persistent panes titled "Cursor" and "Hover" (`PreviewPane` each). Bottom: a collapsible history
panel (timeline + read-only reconstructed view). `Adw.HeaderBar` with a primary menu button (menu
per spec section 27) and a "History" toggle.

Mutation interception (`editor.py`): connect `insert-text` and `delete-range` on the buffer
BEFORE the default handler (plain `connect`) to read the offset and the affected text, then call
`store.apply(Op(...))` from within an idle handler? No: call it synchronously inside the signal
handler so ordering is exact, but guard against re-entrancy and suppress logging while loading
the initial text (`loading` flag). Programmatic edits from commands (restore, insert token, paste)
go through the buffer so they are intercepted the same way. Verify the store text equals the
buffer text after each op in debug mode (`SCRATCHPAD_DEBUG=1`).

Editor commands (menu + palette + shortcuts): undo/redo (Ctrl+Z/Ctrl+Shift+Z), indent/outdent
(Tab/Shift+Tab with selection or on current line), move line/block up/down (Alt+Up/Down), delete
current bullet/block (Ctrl+Shift+K), newline keeps indentation and continues `- ` bullets
(Shift+Enter inserts a plain newline), paste (Ctrl+V), paste as attachment (Ctrl+Shift+V), attach
file (Ctrl+Shift+A), screenshot (Ctrl+Shift+S), clear scratchpad (menu only, with confirmation),
export current state (file dialog), command palette (Ctrl+Shift+P), toggle history (Ctrl+H),
hide window (Escape). Large paste guardrail: if a normal paste exceeds
`config.large_paste_threshold_lines` (default 2000) or `large_paste_threshold_chars` (default 200000),
ask: `[Insert inline] [Store as attachment]`. Paste-as-attachment never asks. Image clipboard on
normal paste creates an image attachment.

Hover preview: on pointer motion over the TextView compute the iter at the location, use
`TokenCodec.candidate_at`, resolve, and update the Hover pane (blank when nothing valid; the pane
is always present). Cursor preview: on `notify::cursor-position` do the same for the cursor. Cache
resolutions per token.

History mode: the history panel shows `TimelineWidget`; selecting a time calls
`store.history.reconstruct_at` and shows the text read-only in a `LineNumberedTextView`; buttons:
Copy text, Copy selection, Restore selection at cursor, Restore entire state (confirm),
Compare with current (opens `DiffView`), Redact selection from history (dialog with the three
options: this occurrence / exact content everywhere / purge attachment when the selection is a
valid token). Historical views never mutate history.

Global toggle: COSMIC keybindings run a command; `scratchpad install-shortcut` writes
`~/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom` (RON) mapping the chosen key to
`Spawn("scratchpad toggle")`, backing up an existing file first, and prints what it did. The
README documents manual setup for KDE/GNOME too. Window show: `set_startup_id(token)` when a token
was received, `present()`, then `editor.grab_focus()`.

Widget contracts (Agent D2 provides, Agent D1 consumes):

```python
class PreviewPane(Gtk.Box):
    def __init__(self, title: str)
    def show_resolution(self, res: Resolution | None, attachments: AttachmentStore | None) -> None
        # None or ORDINARY -> blank body (title stays). VALID image -> Gtk.Picture scaled to fit.
        # VALID text -> LineNumberedTextView with search + first/last buttons + metadata line (lines, chars, created).
        # VALID file -> metadata (filename, mime, size, created) + "Open externally" (Gio.AppInfo.launch_default_for_uri).
        # MISSING -> warning: "Valid local attachment reference, but the backing object is unavailable or corrupt." + detail.
class TimelineWidget(Gtk.DrawingArea):
    __gsignals__ = {"time-selected": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_INT64,))}
    def set_history(self, history: History) -> None        # reads sessions(), time_range(), activity()
    def set_selected(self, wall_ns: int) -> None
    def refresh(self) -> None                                # re-read sessions/activity (called after new events)
    # Draw: running intervals as solid bars, non-running gaps hatched/grey, activity density as a
    # heat strip, tick labels with local time, a selection cursor. Click or drag scrubs and emits
    # time-selected; scroll wheel zooms around the pointer; drag with middle button pans; Home/End keys.
class LineNumberedTextView(Gtk.Box):
    buffer: Gtk.TextBuffer  (read-only view by default)
    def set_text(self, text: str) -> None
    def set_editable(self, editable: bool) -> None
    def search(self, needle: str, *, forward=True) -> bool
    def scroll_to_start(self) / scroll_to_end(self)
class DiffView(Gtk.Box):
    def set_texts(self, old: str, new: str, *, old_label: str, new_label: str) -> None   # unified diff, colored
```

## 13. Configuration (`config.toml`, Agent A `config.py`)

```toml
[storage]
flush_interval_ms = 500
checkpoint_every_events = 2000
heartbeat_seconds = 30
keep_checkpoints = 20
[editor]
font = "monospace 11"
large_paste_threshold_lines = 2000
large_paste_threshold_chars = 200000
continue_bullets = true
[ui]
width = 900
height = 600
```

`Config.load(path | None) -> Config` returns defaults when the file is absent; unknown keys are
ignored with a warning to stderr.

## 14. Testing expectations

- Core: pytest with hypothesis where useful. Crash tests: truncate the log at every byte offset of
  the last record and assert recovery; flip bytes and assert the torn/corrupt tail is detected.
- Acceptance criteria 2 to 7, 9 to 13, 18, 19, 21 from spec section 39 must each have a test.
- UI: a smoke test that starts the app with `SCRATCHPAD_DATA_DIR` pointing at a temp dir, waits
  1 s via `GLib.timeout_add`, types a few characters programmatically into the buffer, quits, and
  asserts the events exist in the log. This session has a live Wayland display, so GTK can start.
- Everything under `tests/` must pass with `.venv/bin/python -m pytest -q`.

## 15. Coding conventions

- Python 3.12+ syntax, `from __future__ import annotations`, dataclasses, `pathlib`.
- No global mutable state except module constants. Logging via `logging.getLogger(__name__)`.
- Fsync discipline: every durable write is temp file, fsync, rename, fsync directory.
- Never swallow exceptions silently in storage code. Never delete user data without an explicit
  operation that the spec defines as destructive.
- Docstrings on public classes and functions. Keep functions short.
