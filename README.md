# Scratchpad

A single, always-persistent plain-text scratchpad with exact temporal history and lightweight
external attachments. There is no save command and no document management: the window holds one
plain-text document that is always there, every edit is recorded as an event so any past state can
be reconstructed on a continuous timeline, large pastes and files live outside the document and are
represented by opaque fixed-length tokens, two passive preview panes show whatever token the cursor
or the mouse pointer is on, and a set of explicit capture commands (paste as attachment, attach
file, screenshot, CLI piping) plus an optional destructive redaction round it off. The application
optimizes for predictability, immediacy and a lack of organizational friction.

## Requirements

Manjaro / Arch packages:

```bash
sudo pacman -S python python-gobject gtk4 libadwaita python-cryptography
```

Optional:

* `xdg-desktop-portal-cosmic` (or another `xdg-desktop-portal` backend) for the screenshot command.
  Without a portal the application falls back to the `cosmic-screenshot` command line tool, and
  without either the screenshot command reports that it is unavailable.

Nothing else is needed: the application uses the Python standard library, PyGObject and
`cryptography` only. GTK 4 and libadwaita are used through PyGObject, so the GTK and libadwaita
typelibs must come from the system packages above.

## Installation

### a) Run from the repository (development, verified)

The repository ships a virtual environment created with system site packages, so the system
PyGObject and `cryptography` are used instead of rebuilt copies:

```bash
cd /home/niklas/Code/scratchpad
uv pip install --python .venv/bin/python --no-deps -e .
.venv/bin/scratchpad --help
```

`--no-deps` matters. Without it, `uv pip install` builds its own PyGObject into the virtual
environment (3.58.0 at the time of writing) which then shadows the system build; both work, but the
system build is the one the application is tested against.

You can also run the code without installing anything:

```bash
.venv/bin/python -m scratchpad gui
```

### b) Install for your user with pipx (verified)

```bash
cd /home/niklas/Code/scratchpad
pipx install --system-site-packages .
```

This creates `~/.local/bin/scratchpad`, `~/.local/bin/scratchpad-attach` and
`~/.local/bin/scratchpad-insert`. Verified on this machine: the GUI starts, `scratchpad-insert` and
`scratchpad-attach` work and the process shuts down cleanly on SIGTERM.

Note that pipx still builds its own PyGObject and `cryptography` copies inside the pipx virtual
environment even with `--system-site-packages`, so the build toolchain (`base-devel`,
`gobject-introspection`, `cairo`) has to be installed for the command to succeed. Passing
`--pip-args='--no-deps'` does not help with pipx 1.15, which already passes that flag to its uv
backend and then fails with "the argument '--no-deps' cannot be used multiple times".

`uv tool install` is not usable here: there is no `--with-system-site-packages` (or equivalent)
option in uv 0.12.10, so a uv tool environment cannot see the system PyGObject.

### c) Desktop entry

```bash
cp data/dev.scratchpad.Scratchpad.desktop ~/.local/share/applications/
```

Adjust `Exec=` first if the `scratchpad` command is not on your `PATH`, for example
`Exec=/home/niklas/Code/scratchpad/.venv/bin/scratchpad`. The entry sets
`StartupWMClass=dev.scratchpad.Scratchpad`, which is the application id the window uses, so the
window is matched to the launcher.

## First start

```bash
scratchpad
```

With no arguments the command toggles a running instance, and starts the GUI when none is running.
The first start creates the data directory (`~/.local/share/scratchpad` by default), the secret key
and an empty document. The window can be closed at any time: closing only hides it, the process
keeps running so the global shortcut can bring it back, and `scratchpad` or `scratchpad toggle`
raises it again. Only *Quit* (Ctrl+Q), SIGINT or SIGTERM end the process, and all three write a
clean end-of-session marker, a checkpoint and `current.txt`.

Only one instance may run per data directory: the data directory is locked with `flock`, and a
second `scratchpad gui` asks the running instance to show itself and exits with status 0.

## Global shortcut

On COSMIC:

```bash
scratchpad install-shortcut --key "Super+grave"
```

This writes `~/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom`, backing up an
existing file first, and prints what it did and where the backup went. Use `--replace` to take over
a key combination that is already bound to something else, and `--command` to bind something other
than `scratchpad toggle`. Log out and back in (or restart `cosmic-comp`) if the shortcut does not
react.

On other desktops, bind the command `scratchpad toggle` yourself:

* KDE Plasma: System Settings, Shortcuts, Custom, add a new command shortcut for
  `scratchpad toggle` and assign the key.
* GNOME: Settings, Keyboard, View and Customize Shortcuts, Custom Shortcuts, add
  `scratchpad toggle` and assign the key.
* Xfce: Settings, Keyboard, Application Shortcuts, add `scratchpad toggle`.

The same command run from a terminal or a launcher works too, so anything that can run a command
can drive the scratchpad.

## Keyboard shortcuts

| Key | Action |
|---|---|
| Ctrl+Z | Undo (recorded as an ordinary edit, it does not rewind history) |
| Ctrl+Shift+Z | Redo |
| Tab | Indent the current line or the selected block |
| Shift+Tab | Outdent the current line or the selected block |
| Alt+Up | Move the current line or block up |
| Alt+Down | Move the current line or block down |
| Ctrl+Shift+K | Delete the current bullet or block (the line plus deeper indented lines) |
| Enter | New line, keeping the indentation and continuing `- ` bullets |
| Shift+Enter | Plain new line |
| Ctrl+V | Paste inline (asks about very large pastes, see below) |
| Ctrl+Shift+V | Paste as attachment (never asks) |
| Ctrl+Shift+A | Attach a file |
| Ctrl+Shift+S | Screenshot into an image attachment |
| Ctrl+E | Export the current state to a file |
| Ctrl+H | Show or hide the history panel |
| Ctrl+Shift+P | Command palette |
| Ctrl+Q | Quit (ends the process) |
| Escape | Hide the window (the process keeps running) |

In the command palette: type to filter, Up and Down or Page Up and Page Down to move, Enter to run,
Escape to close. On the timeline: click or drag to scrub, Left and Right to step, Home and End to
jump to the ends of the visible range, the scroll wheel zooms around the pointer and dragging with
the middle button pans.

Everything in the table is also reachable from the primary menu (File, Edit, Insert, History, View,
Commands) and from the command palette, including the commands that have no shortcut at all: clear
the scratchpad, attach the clipboard image, toggle the preview panes, install the global shortcut,
and the three destructive history commands.

A normal Ctrl+V that exceeds `large_paste_threshold_lines` (2000) or `large_paste_threshold_chars`
(200000) asks whether to insert it inline or to store it as an attachment. Pasting an image with
Ctrl+V always creates an image attachment, because an image cannot be plain text.

## Command line

### `scratchpad`

```
usage: scratchpad [-h] [--version] [--socket PATH] COMMAND ...

Persistent scratchpad: toggle the window or drive the running instance.

positional arguments:
  COMMAND
    toggle            show the window if hidden, hide it otherwise
    show              show and focus the window
    hide              hide the window (does not start the GUI)
    status            print the state of the running instance
    screenshot        capture a screenshot into an attachment
    paste-clipboard   show the window and paste the clipboard
    gui               run the GUI in the foreground
    install-shortcut  register the global toggle shortcut (COSMIC)

options:
  -h, --help          show this help message and exit
  --version           show program's version number and exit
  --socket PATH       path of the IPC socket (default:
                      $XDG_RUNTIME_DIR/scratchpad/ipc.sock)

With no command: toggle the running instance, or start it when none is
running.
```

```
usage: scratchpad install-shortcut [-h] [--key KEY] [--command CMD]
                                   [--replace]

options:
  -h, --help     show this help message and exit
  --key KEY      key combination, e.g. 'Ctrl+Alt+space' (default: Super+grave)
  --command CMD  command the shortcut runs (default: 'scratchpad toggle')
  --replace      overwrite an existing binding that already uses the same key
                 combination
```

### `scratchpad-attach`

```
usage: scratchpad-attach [-h] [--mime M] [--name N] [--socket PATH] [FILE]

Store FILE or stdin as an attachment and insert its token at the cursor.

positional arguments:
  FILE           input file ('-' or omitted: stdin)

options:
  -h, --help     show this help message and exit
  --mime M       content type (default: guessed)
  --name N       file name recorded with the attachment
  --socket PATH  path of the IPC socket
```

### `scratchpad-insert`

```
usage: scratchpad-insert [-h] [--end] [--socket PATH] [TEXT ...]

Insert TEXT (or stdin) into the scratchpad at the cursor.

positional arguments:
  TEXT           text to insert; read stdin when omitted

options:
  -h, --help     show this help message and exit
  --end          append at the end of the document instead
  --socket PATH  path of the IPC socket
```

### Examples

```bash
journalctl -b | scratchpad-attach          # prints the 22 character token it inserted
echo "temporary reminder" | scratchpad-insert
scratchpad-insert --end "appended at the end of the document"
scratchpad-attach --name build.log --mime text/plain build.log
scratchpad status
```

All of these talk to the running GUI over a Unix socket at
`$XDG_RUNTIME_DIR/scratchpad/ipc.sock`. When nothing is listening, every command except `hide` and
`status` starts the GUI and retries, so piping into a cold machine works.

## Configuration

`$XDG_CONFIG_HOME/scratchpad/config.toml`, by default `~/.config/scratchpad/config.toml`. The file
is optional and so is every key; a missing or malformed file falls back to the defaults, and
unknown sections or keys and values of the wrong type are ignored with a warning on stderr. Numeric
values outside their sensible range are clamped, also with a warning. The defaults are:

```toml
[storage]
flush_interval_ms = 500          # how often buffered events are fsynced (0: on every 250 ms tick)
checkpoint_every_events = 2000   # write a checkpoint after this many events
heartbeat_seconds = 30           # heartbeat interval, bounds how much of a crashed session is unknown
keep_checkpoints = 20            # how many checkpoint files to keep

[editor]
font = "monospace 11"            # Pango font description for the editor
large_paste_threshold_lines = 2000     # a larger normal paste asks inline or attachment
large_paste_threshold_chars = 200000   # same, in characters
continue_bullets = true          # Enter continues "- " bullet lines

[ui]
width = 900                      # initial window width
height = 600                     # initial window height
```

The configuration is read once at start-up.

## Data directory

`$SCRATCHPAD_DATA_DIR` if set, else `$XDG_DATA_HOME/scratchpad`, by default
`~/.local/share/scratchpad`. Directories are created with mode 0700 and files with mode 0600.

```
<data_dir>/
  lock                       locked with flock by the running instance (single instance enforcement)
  secret.key                 32 random bytes, the installation key that tokens authenticate against
  current.txt                latest durable full text, plain UTF-8, safe to cat
  current.meta               which log position current.txt belongs to
  history/
    events.log               the append-only event log, the only authoritative file
    checkpoints/
      <seq>.ckpt             full document text at that event sequence number, zlib compressed
    index.cache              optional, rebuildable time index (may be absent)
  attachments/
    attachments.sqlite       attachment metadata: object id, token, kind, mime, sha256, size, ...
    blobs/<xx>/<sha256>      the immutable attachment contents, addressed by content hash
```

`events.log` is the source of truth. `current.txt`, `current.meta`, the checkpoints and
`index.cache` are caches: deleting them costs start-up time, not data. `secret.key` and
`attachments/` are not caches.

## History, checkpoints and the timeline

Every mutation of the document becomes exactly one event in `history/events.log`, which is
append-only: typing `hello` character by character produces five insert events and therefore the
historical states `h`, `he`, `hel`, `hell` and `hello`, while pasting `hello` produces one. Deleted
text is stored in the event, so a deletion is reversible by replay, and undo and redo are ordinary
edits that are recorded like any other. The log also records when the application starts and stops,
plus a heartbeat every `heartbeat_seconds` and wall-clock anchors, so the timeline knows which
intervals the application was actually running in and which it was not, even after a crash. Records
are checksummed, and a torn tail from a power loss is detected and truncated at the next start
instead of corrupting the history.

Reconstructing the document at an arbitrary instant means taking the newest checkpoint at or before
that point and replaying the events that follow it. Checkpoints are written every
`checkpoint_every_events` events and at a clean shutdown, and `keep_checkpoints` of them are kept;
they are purely an accelerator, so deleting them all loses nothing. The history panel (Ctrl+H)
shows the timeline at the bottom of the window: running intervals are drawn as solid bars, the gaps
where the application was not running are hatched, edit density is drawn as a heat strip, and
clicking or scrubbing selects an instant. The selected state appears below the timeline as a
read-only view with line numbers. From there you can copy the whole text or the selection, insert
the selection at the cursor, restore the whole state into the current document, or compare the
state with the current document as a unified diff. None of this modifies history: restoring an old
state is recorded as one ordinary replacement edit, so the document you had before the restore
stays reachable. Clearing the scratchpad is likewise an ordinary deletion of everything, and the
cleared content remains in the history until it is explicitly redacted.

## Attachments and tokens

Large logs, files, pasted images and screenshots are not put into the document. They are stored in
`attachments/` (metadata in sqlite, contents as an immutable blob named after their SHA-256 hash,
so identical content is stored once) and the document only receives a token: exactly 22 characters
from `0-9A-Za-z`, inserted as ordinary text with a space on either side if needed to keep it a
separate word. The document therefore stays plain text that any other tool can read. Tokens are
created by `scratchpad-attach`, by Paste as attachment (Ctrl+Shift+V), by Attach file
(Ctrl+Shift+A), by the screenshot command, and automatically when an image is pasted or a normal
paste is too large and you choose to store it. A token is the encrypted form of an object id under
the installation key in `secret.key`, so consecutive attachments get visually unrelated tokens and
a token carries no readable structure.

Whenever the cursor moves onto a token, or the mouse pointer hovers over one, the matching preview
pane on the right updates: images are shown scaled, text and logs are shown with line numbers,
search and the metadata line, other files show their name, type, size and creation time plus an
"Open externally" button. The panes are always present and simply go blank when there is nothing to
show, and previewing never modifies the document. There are three token states. A candidate that
does not authenticate against this installation's key is *ordinary text* and is left alone, which
is also what happens to a token issued by another installation or by another machine: it is 22
harmless alphanumeric characters and nothing else (random 22-character strings almost never
authenticate). A token that authenticates and whose object exists is *valid* and is previewed. A
token that authenticates but whose metadata row or blob is gone or corrupt is *missing*, and the
pane says so explicitly: "Valid local attachment reference, but the backing object is unavailable
or corrupt." Deleting a token from the current document never deletes the attachment, because
historical states still contain it.

## Redaction and what deletion means here

Normal deletion preserves history, on purpose. For the case where something must really go, an
accidentally pasted API key, a password, a confidential paragraph or an unwanted screenshot, the
History menu and the command palette offer three explicit operations: *Redact this historical
occurrence* (only the run of states around the occurrence you selected in a reconstructed state),
*Redact this exact content everywhere* (every retained state), and *Purge this attachment from
history* (the token everywhere, then the metadata row and the blob). Each one first shows a dry run
that changes nothing and reports exactly how many occurrences and states would be affected and how
the event log would shrink, and only a second, explicitly destructive confirmation carries it out.
The rewrite replays the log, writes a complete new log next to the old one, fsyncs it and swaps it
in atomically behind a journal, so a crash in the middle leaves either the old history or the new
one and never a half-rewritten file. A separate command, *Collect unreferenced attachments*, scans
every retained state and deletes only those attachments that no state mentions any more.

The wording is deliberate. The operation is called:

```text
Remove permanently from scratchpad history
```

and never:

```text
Securely erase from physical storage
```

The application does not claim forensic secure deletion, and it should not: crash persistence
produces multiple durable copies over time, and removed material may still exist in filesystem
snapshots, backups, SSD flash translation layers, swap, crash dumps and stale filesystem blocks
outside this application's control. Every destructive dialog says so.

## Backups

Back up the whole data directory as one unit, while the application is not running if you can, and
restore it as one unit. The event log, the checkpoints, the attachment database and the blobs
belong together.

Two details matter:

* `secret.key` is required to validate tokens. If it is lost, the next start simply creates a new
  one, and the tokens already in your document no longer authenticate against it: the application
  treats them as ordinary text and their previews stop working, even though the attachment data
  itself is untouched. The attachment database keeps the token string in a `token` column as the
  fallback for exactly that case: an `AttachmentStore` opened without a key resolves through that
  column instead, and the resolution says that it did (`resolved via stored token; secret key
  unavailable`). The GUI always uses the key, so back up `secret.key` with the rest, and treat it
  as exactly as sensitive as the attachments it protects.
* Backing up `history/events.log` alone is enough to reconstruct every text state, but not the
  attachment contents, which live in `attachments/blobs/`.

Note that a backup is also the reason redaction cannot promise more than it does: a copy that was
already taken is outside the application's reach.

## Development

```bash
cd /home/niklas/Code/scratchpad
.venv/bin/python -m pytest -q             # the whole suite
.venv/bin/python -m pytest -q tests/test_store.py
SCRATCHPAD_DEBUG=1 .venv/bin/python -m scratchpad gui   # debug logging and per-op store checks
```

The tests that need GTK skip themselves when no display is available. Tests that start a real GUI
process always point `SCRATCHPAD_DATA_DIR` and `XDG_RUNTIME_DIR` at a temporary directory; do the
same for any manual experiment so that your real scratchpad is never touched:

```bash
export SCRATCHPAD_DATA_DIR=$(mktemp -d) XDG_RUNTIME_DIR=$(mktemp -d)
```

`ARCHITECTURE.md` is the binding contract between the modules (file formats, interfaces, invariants)
and `Design Specification.md` holds the product requirements. Read the specification first, then the
architecture document.

Module map:

| Module | Contents |
|---|---|
| `scratchpad/paths.py` | data, config and runtime directory resolution, atomic writes, fsync helpers |
| `scratchpad/config.py` | `config.toml` loading, defaults, clamping |
| `scratchpad/core/varint.py` | LEB128 varints used by the log format |
| `scratchpad/core/clock.py` | monotonic and wall clock anchoring |
| `scratchpad/core/ops.py` | `Op`, `Event`, `apply_op`, `invert_op` |
| `scratchpad/core/eventlog.py` | binary log format, writer, reader, torn tail detection |
| `scratchpad/core/checkpoint.py` | checkpoint files |
| `scratchpad/core/history.py` | sessions, time index, reconstruction, activity |
| `scratchpad/core/store.py` | `ScratchpadStore`, the facade the UI uses |
| `scratchpad/core/recovery.py` | finishing or discarding an interrupted history rewrite |
| `scratchpad/tokens.py` | Base62, keyed permutation, MAC, `TokenCodec`, key management |
| `scratchpad/attachments.py` | `AttachmentStore`, blobs, resolution states |
| `scratchpad/ipc.py` | the socket protocol, `IpcServer`, `IpcClient` |
| `scratchpad/cli.py` | `scratchpad`, `scratchpad-attach`, `scratchpad-insert`, shortcut installation |
| `scratchpad/redaction.py` | history rewrite, redact, purge |
| `scratchpad/gc.py` | attachment reference scan and blob collection |
| `scratchpad/textdiff.py` | minimal diff between two texts |
| `scratchpad/ui/app.py` | the application object, single instance, IPC wiring, timers |
| `scratchpad/ui/window.py` | the window, panes, menu, history mode, IPC handler |
| `scratchpad/ui/editor.py` | the text view and the mutation interception that feeds the log |
| `scratchpad/ui/commands.py` | the command registry shared by menu, palette and shortcuts |
| `scratchpad/ui/palette.py` | the command palette |
| `scratchpad/ui/capture.py` | clipboard, paste guardrail, file attachment, screenshot |
| `scratchpad/ui/dialogs.py` | confirmations, the large-paste dialog, redaction wording, reports |
| `scratchpad/ui/preview.py` | the two preview panes |
| `scratchpad/ui/timeline.py` | the timeline widget |
| `scratchpad/ui/textview_extras.py` | the line-numbered read-only view with search |
| `scratchpad/ui/diffview.py` | the unified diff view |
