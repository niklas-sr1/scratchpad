"""Command line front ends: ``scratchpad``, ``scratchpad-attach``, ``scratchpad-insert``.

The CLI never touches the event log or the attachment store directly.  It talks
to the running GUI over the Unix socket protocol in :mod:`scratchpad.ipc` and,
when no instance is running, starts one and retries.

No ``gi`` import happens here: ``scratchpad gui`` imports :mod:`scratchpad.ui.app`
lazily so that the CLI stays usable (and testable) without PyGObject.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from scratchpad import __version__
from scratchpad.ipc import (
    DEFAULT_CLIENT_TIMEOUT,
    IpcClient,
    IpcError,
    IpcUnavailable,
    is_server_alive,
)

__all__ = [
    "main",
    "attach_main",
    "insert_main",
    "install_cosmic_shortcut",
    "ShortcutConflict",
]

#: How long the CLI waits for a freshly spawned GUI to answer, in seconds.
START_TIMEOUT = 8.0

#: Poll interval while waiting for the GUI, in seconds.
START_POLL = 0.1

#: Requests carrying a payload get a roomier deadline than the default.
PAYLOAD_TIMEOUT = 60.0

#: Default key for ``scratchpad install-shortcut``.
DEFAULT_SHORTCUT_KEY = "Super+grave"

#: Command the shortcut runs.
SHORTCUT_COMMAND = "scratchpad toggle"

#: Relative location of the COSMIC custom shortcut file below the config dir.
COSMIC_SHORTCUT_RELPATH = Path("cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom")

#: Where backups of that file go.  Not next to it: cosmic-config treats *every* file
#: name in ``.../Shortcuts/v1/`` as a configuration key.
COSMIC_BACKUP_RELDIR = Path("cosmic/scratchpad-shortcut-backups")

#: Above this many UTF-8 bytes ``scratchpad-insert`` sends the text as the binary
#: payload instead of in the JSON header (which :data:`ipc.MAX_HEADER_BYTES` caps).
INSERT_PAYLOAD_THRESHOLD = 64 << 10

#: How much of a piped file is inspected for NUL bytes when nothing declares its type.
SNIFF_BYTES = 8 << 10

#: Commands that carry the Wayland activation token (see ARCHITECTURE.md section 8).
_ACTIVATION_COMMANDS = frozenset({"toggle", "show", "hide", "paste_clipboard"})

_MODIFIER_ALIASES = {
    "super": "Super",
    "meta": "Super",
    "mod4": "Super",
    "win": "Super",
    "windows": "Super",
    "logo": "Super",
    "cmd": "Super",
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "primary": "Ctrl",
    "alt": "Alt",
    "mod1": "Alt",
    "option": "Alt",
    "shift": "Shift",
}

#: RON emits the modifier list in this order so repeated installs are stable.
_MODIFIER_ORDER = ("Super", "Ctrl", "Alt", "Shift")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _activation_token() -> str | None:
    token = os.environ.get("XDG_ACTIVATION_TOKEN")
    return token or None


def _with_activation(request: dict[str, Any]) -> dict[str, Any]:
    """Add ``activation_token`` when the environment provides one."""
    if request.get("cmd") in _ACTIVATION_COMMANDS:
        token = _activation_token()
        if token:
            request["activation_token"] = token
    return request


#: Single use startup notification tokens.  They belong to *this* invocation and are
#: passed to the GUI in the request instead; inherited into a long lived process they
#: go stale and confuse the compositor's focus handling for the rest of the session.
_STARTUP_ENV_VARS = ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID")

#: ...except that a cold started GUI has no request to carry the token: ``toggle``
#: does not resend itself once the window is up (it is already on screen).  So the
#: token travels in this private variable instead, which ``scratchpad.ui.app`` pops
#: on the first activation and hands to ``Gtk.Window.set_startup_id``; without it
#: the first press of the global shortcut only makes the window demand attention.
ACTIVATION_TOKEN_ENV = "SCRATCHPAD_ACTIVATION_TOKEN"


def _gui_environment() -> dict[str, str]:
    """A copy of the environment with this invocation's startup tokens rewritten."""
    env = {k: v for k, v in os.environ.items() if k not in _STARTUP_ENV_VARS}
    env.pop(ACTIVATION_TOKEN_ENV, None)
    token = _activation_token()
    if token:
        env[ACTIVATION_TOKEN_ENV] = token
    return env


def _spawn_gui() -> subprocess.Popen[bytes]:
    """Start the GUI detached from this process."""
    return subprocess.Popen(
        [sys.executable, "-m", "scratchpad", "gui"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=_gui_environment(),
    )


def _wait_for_server(socket_path: str | None, proc: subprocess.Popen[bytes] | None) -> bool:
    """Poll the socket until it answers ``ping`` or :data:`START_TIMEOUT` elapses."""
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if is_server_alive(socket_path, timeout=1.0):
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(START_POLL)
    return is_server_alive(socket_path, timeout=1.0)


def _client(socket_path: str | None) -> IpcClient:
    return IpcClient(socket_path)


def _send(
    request: dict[str, Any],
    payload: bytes | None = None,
    *,
    socket_path: str | None = None,
    timeout: float = DEFAULT_CLIENT_TIMEOUT,
    autostart: bool = True,
    resend_after_start: bool = True,
) -> dict[str, Any] | None:
    """Send ``request``, starting the GUI when needed.

    Returns the response object, or ``None`` when the request could not be
    delivered (an explanation has been printed to stderr by then).
    """
    request = _with_activation(dict(request))
    try:
        return _client(socket_path).request(request, payload, timeout=timeout)
    except IpcUnavailable:
        if not autostart:
            _err("scratchpad: not running")
            return None
    except IpcError as exc:
        _err(f"scratchpad: {exc}")
        return None

    proc = _spawn_gui()
    if not _wait_for_server(socket_path, proc):
        _err(f"scratchpad: the GUI did not come up within {START_TIMEOUT:g}s")
        return None
    if not resend_after_start:
        return {"ok": True, "started": True}
    try:
        return _client(socket_path).request(_with_activation(dict(request)), payload, timeout=timeout)
    except IpcError as exc:
        _err(f"scratchpad: {exc}")
        return None


def _report(response: dict[str, Any] | None) -> int:
    """Turn a response into a process exit code, printing any error."""
    if response is None:
        return 1
    if not response.get("ok"):
        _err(f"scratchpad: {response.get('error', 'request failed')}")
        return 1
    return 0


def _read_input(path: str | None) -> bytes:
    """Read a file argument (``-`` or missing means stdin) completely."""
    if path is None or path == "-":
        return sys.stdin.buffer.read()
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise SystemExit(f"scratchpad-attach: cannot read {path}: {exc.strerror}")


def _guess_mime(name: str | None, default: str) -> str:
    if name:
        guessed, _ = mimetypes.guess_type(name)
        if guessed:
            return guessed
    return default


def sniff_mime(data: bytes) -> str:
    """Content type of ``data`` when neither a ``--mime`` nor a file name says anything.

    Images are recognised by their magic bytes; anything with NUL bytes near the front
    is binary; the rest is treated as text.  Guessing ``text/plain`` for a piped PNG
    used to store it as a text attachment with nonsense line counts.
    """
    from scratchpad.attachments import sniff_image_mime  # local: keeps CLI startup light

    image = sniff_image_mime(data)
    if image:
        return image
    if b"\x00" in data[:SNIFF_BYTES]:
        return "application/octet-stream"
    return "text/plain"


def kind_for_mime(mime: str | None) -> str:
    """Map a mime type onto the attachment kind used by the ``attach`` command."""
    if not mime or mime.startswith("text/"):
        return "text"
    if mime.startswith("image/"):
        return "image"
    return "file"


# --------------------------------------------------------------------------- #
# COSMIC shortcut installation
# --------------------------------------------------------------------------- #

def _config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".config"


def cosmic_shortcut_path() -> Path:
    """Location of the COSMIC custom shortcut file."""
    return _config_home() / COSMIC_SHORTCUT_RELPATH


def parse_key_spec(spec: str) -> tuple[list[str], str]:
    """Parse ``"Super+grave"`` into ``(["Super"], "grave")``.

    Accepts ``+`` or ``-`` as separators and the usual modifier aliases
    (``super``/``meta``/``win``, ``ctrl``/``control``, ``alt``, ``shift``).
    """
    raw = [part for part in spec.replace("-", "+").split("+") if part.strip()]
    if not raw:
        raise ValueError(f"empty key specification: {spec!r}")
    key = raw[-1].strip()
    modifiers: list[str] = []
    for part in raw[:-1]:
        name = _MODIFIER_ALIASES.get(part.strip().lower())
        if name is None:
            raise ValueError(f"unknown modifier {part.strip()!r} in {spec!r}")
        if name not in modifiers:
            modifiers.append(name)
    if not key:
        raise ValueError(f"no key in {spec!r}")
    if key.lower() in _MODIFIER_ALIASES:
        raise ValueError(f"{key!r} is a modifier, not a key: {spec!r}")
    modifiers.sort(key=_MODIFIER_ORDER.index)
    return modifiers, key


def _binding_line(modifiers: list[str], key: str, command: str, indent: str = "    ") -> str:
    mods = ", ".join(modifiers)
    return f'{indent}(modifiers: [{mods}], key: "{key}"): Spawn("{command}"),'


#: ``(modifiers: [Super, Shift], key: "grave"): Spawn("..."),`` - the left hand side of
#: one RON map entry plus whatever action follows it.
_BINDING_RE = re.compile(
    r'^(?P<indent>\s*)\(\s*modifiers\s*:\s*\[(?P<mods>[^\]]*)\]\s*,\s*'
    r'key\s*:\s*"(?P<key>[^"]*)"\s*\)\s*:\s*(?P<action>.*?),?\s*$'
)


def _parse_binding_line(raw: str) -> tuple[list[str], str, str] | None:
    """Split one RON binding line into ``(modifiers, key, action)``, or ``None``."""
    match = _BINDING_RE.match(raw)
    if match is None:
        return None
    modifiers: list[str] = []
    for part in match.group("mods").split(","):
        name = _MODIFIER_ALIASES.get(part.strip().lower())
        if name is None:
            if part.strip():
                return None  # a modifier we do not understand: leave the line alone
            continue
        if name not in modifiers:
            modifiers.append(name)
    modifiers.sort(key=_MODIFIER_ORDER.index)
    return modifiers, match.group("key"), match.group("action").strip()


class ShortcutConflict(ValueError):
    """The requested key combination is already bound to a different command."""


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` durably, through a uniquely named temp file.

    The temp file is dot prefixed and removed on failure: it shares the directory with
    the target, and in ``.../Shortcuts/v1/`` every left-over file name would show up as
    a configuration key of its own.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def cosmic_backup_dir() -> Path:
    """Directory that keeps copies of the shortcut file from before each change."""
    return _config_home() / COSMIC_BACKUP_RELDIR


def _write_backup(path: Path, original: str) -> Path:
    directory = cosmic_backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = directory / f"{path.name}.bak-{stamp}"
    counter = 1
    while backup.exists():
        backup = directory / f"{path.name}.bak-{stamp}-{counter}"
        counter += 1
    backup.write_text(original, encoding="utf-8")
    return backup


def install_cosmic_shortcut(
    key_spec: str, command: str = SHORTCUT_COMMAND, *, replace: bool = False
) -> str:
    """Merge ``key_spec -> Spawn(command)`` into the COSMIC custom shortcut file.

    Conservative text handling, deliberately not a RON parser: *every* existing entry
    that spawns the same command is dropped and exactly one new entry takes the place
    of the first of them, otherwise the entry is inserted before the final closing
    brace.  A duplicate map key would make cosmic-config reject the file, so a
    combination that is already bound to a *different* command raises
    :class:`ShortcutConflict` and leaves the file untouched unless ``replace`` is set,
    in which case the conflicting entry is overwritten.  Any pre-existing file is
    copied into :func:`cosmic_backup_dir` first and the new content is written
    atomically.  Returns a human readable summary of what happened.
    """
    modifiers, key = parse_key_spec(key_spec)
    path = cosmic_shortcut_path()
    line = _binding_line(modifiers, key, command)
    spawn_needle = f'Spawn("{command}")'
    notes: list[str] = []

    original = ""
    if path.exists():
        original = path.read_text(encoding="utf-8")

    if not original.strip():
        new_text = "{\n" + line + "\n}\n"
        action = "created"
    else:
        out: list[str] = []
        conflicts: list[str] = []
        anchor: int | None = None
        indent = "    "
        for raw in original.splitlines():
            parsed = _parse_binding_line(raw)
            ours = spawn_needle in raw and "):" in raw
            clashes = (
                parsed is not None
                and not ours
                and parsed[0] == modifiers
                and parsed[1] == key
            )
            if clashes:
                conflicts.append(raw.strip())
            if ours or (clashes and replace):
                if anchor is None:
                    anchor = len(out)
                    indent = raw[: len(raw) - len(raw.lstrip())] or "    "
                continue  # drop it; exactly one replacement goes in at the anchor
            out.append(raw)

        if conflicts and not replace:
            joined = "\n  ".join(conflicts)
            raise ShortcutConflict(
                f"{key_spec} is already bound to something else in {path}:\n  {joined}\n"
                f"  left untouched; pass --replace to overwrite that binding"
            )
        if conflicts:
            notes.append(f"replaced a conflicting binding: {conflicts[0]}")

        if anchor is not None:
            out.insert(anchor, _binding_line(modifiers, key, command, indent))
            action = "updated"
        else:
            close = -1
            for index in range(len(out) - 1, -1, -1):
                if out[index].strip().startswith("}"):
                    close = index
                    break
            if close < 0:
                raise ValueError(
                    f"{path} does not look like a RON map (no closing brace); left untouched"
                )
            out.insert(close, line)
            action = "added a binding to"
        new_text = "\n".join(out) + "\n"

    backup: Path | None = None
    if original:
        if new_text == original:
            return f"{path} already contains {line.strip()} - nothing to do."
        backup = _write_backup(path, original)

    _atomic_write(path, new_text)

    message = f"{action} {path}\n  {line.strip()}"
    if backup is not None:
        message += f"\n  backup: {backup}"
    for note in notes:
        message += f"\n  {note}"
    message += "\n  Log out and back in (or restart cosmic-comp) if the shortcut does not react."
    return message


def _desktop_instructions(key_spec: str, command: str) -> str:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") or "unknown"
    lower = desktop.lower()
    if "kde" in lower or "plasma" in lower:
        where = "KDE: System Settings > Shortcuts > Custom"
    elif "gnome" in lower:
        where = "GNOME: Settings > Keyboard > Custom Shortcuts"
    elif "xfce" in lower:
        where = "Xfce: Settings > Keyboard > Application Shortcuts"
    else:
        where = "your desktop's keyboard shortcut settings"
    return (
        f"This is not a COSMIC session (XDG_CURRENT_DESKTOP={desktop}).\n"
        f"Add the global shortcut manually:\n"
        f"  {where}\n"
        f"  Shortcut: {key_spec}\n"
        f"  Command:  {command}"
    )


# --------------------------------------------------------------------------- #
# scratchpad
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scratchpad",
        description="Persistent scratchpad: toggle the window or drive the running instance.",
        epilog="With no command: toggle the running instance, or start it when none is running.",
    )
    parser.add_argument("--version", action="version", version=f"scratchpad {__version__}")
    parser.add_argument(
        "--socket",
        metavar="PATH",
        default=None,
        help="path of the IPC socket (default: $XDG_RUNTIME_DIR/scratchpad/ipc.sock)",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    subparsers.add_parser("toggle", help="show the window if hidden, hide it otherwise")
    subparsers.add_parser("show", help="show and focus the window")
    subparsers.add_parser("hide", help="hide the window (does not start the GUI)")
    subparsers.add_parser("status", help="print the state of the running instance")
    subparsers.add_parser("screenshot", help="capture a screenshot into an attachment")
    subparsers.add_parser("paste-clipboard", help="show the window and paste the clipboard")
    subparsers.add_parser("gui", help="run the GUI in the foreground")

    shortcut = subparsers.add_parser(
        "install-shortcut", help="register the global toggle shortcut (COSMIC)"
    )
    shortcut.add_argument(
        "--key",
        default=DEFAULT_SHORTCUT_KEY,
        metavar="KEY",
        help=f"key combination, e.g. 'Ctrl+Alt+space' (default: {DEFAULT_SHORTCUT_KEY})",
    )
    shortcut.add_argument(
        "--command",
        dest="spawn_command",
        default=SHORTCUT_COMMAND,
        metavar="CMD",
        help=f"command the shortcut runs (default: {SHORTCUT_COMMAND!r})",
    )
    shortcut.add_argument(
        "--replace",
        action="store_true",
        help="overwrite an existing binding that already uses the same key combination",
    )
    return parser


def _cmd_gui() -> int:
    """Run the GTK application in the foreground (lazy import, keeps gi optional)."""
    try:
        from scratchpad.ui.app import main as ui_main
    except ImportError as exc:
        _err(f"scratchpad: cannot start the GUI: {exc}")
        return 1
    result = ui_main()
    return 0 if result is None else int(result)


def _cmd_status(socket_path: str | None) -> int:
    response = _send(
        {"cmd": "status"},
        socket_path=socket_path,
        autostart=False,
    )
    if response is None:
        return 1
    if not response.get("ok"):
        _err(f"scratchpad: {response.get('error', 'request failed')}")
        return 1
    known = (
        ("running", lambda _r: "yes"),
        ("visible", lambda r: "yes" if r.get("visible") else "no"),
        ("chars", lambda r: str(r.get("chars", "?"))),
        ("events", lambda r: str(r.get("events", "?"))),
        ("version", lambda r: str(r.get("version", "?"))),
    )
    labels = {
        "running": "running",
        "visible": "window visible",
        "chars": "characters",
        "events": "events",
        "version": "version",
    }
    shown = {"ok"}
    for field, render in known:
        if field != "running" and field not in response:
            continue
        shown.add(field)
        print(f"{labels[field]:>16}: {render(response)}")
    for field in sorted(response):
        if field in shown:
            continue
        print(f"{field:>16}: {response[field]}")
    return 0


def _cmd_install_shortcut(key: str, command: str, replace: bool = False) -> int:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if "cosmic" not in desktop.lower():
        print(_desktop_instructions(key, command))
        return 0
    try:
        print(install_cosmic_shortcut(key, command, replace=replace))
    except (ValueError, OSError) as exc:
        _err(f"scratchpad: {exc}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``scratchpad`` command."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    command: str | None = args.command
    socket_path: str | None = args.socket

    if command == "gui":
        return _cmd_gui()
    if command == "install-shortcut":
        return _cmd_install_shortcut(args.key, args.spawn_command, args.replace)
    if command == "status":
        return _cmd_status(socket_path)
    if command == "hide":
        return _report(_send({"cmd": "hide"}, socket_path=socket_path, autostart=False))
    if command in (None, "toggle"):
        # Starting the GUI already puts the window on screen, so do not toggle
        # it straight back off after an autostart.
        return _report(
            _send(
                {"cmd": "toggle"},
                socket_path=socket_path,
                resend_after_start=False,
            )
        )
    if command == "show":
        return _report(_send({"cmd": "show"}, socket_path=socket_path))
    if command == "screenshot":
        return _report(_send({"cmd": "screenshot"}, socket_path=socket_path))
    if command == "paste-clipboard":
        return _report(_send({"cmd": "paste_clipboard"}, socket_path=socket_path))
    parser.error(f"unknown command: {command}")  # pragma: no cover - argparse guards
    return 2


# --------------------------------------------------------------------------- #
# scratchpad-attach
# --------------------------------------------------------------------------- #

def attach_main(argv: list[str] | None = None) -> int:
    """Entry point of ``scratchpad-attach``: pipe data into an attachment."""
    parser = argparse.ArgumentParser(
        prog="scratchpad-attach",
        description="Store FILE or stdin as an attachment and insert its token at the cursor.",
    )
    parser.add_argument("--mime", metavar="M", default=None, help="content type (default: guessed)")
    parser.add_argument("--name", metavar="N", default=None, help="file name recorded with the attachment")
    parser.add_argument("--socket", metavar="PATH", default=None, help="path of the IPC socket")
    parser.add_argument("file", metavar="FILE", nargs="?", default=None, help="input file ('-' or omitted: stdin)")
    args = parser.parse_args(argv)

    from_stdin = args.file is None or args.file == "-"
    data = _read_input(args.file)
    if not data:
        _err("scratchpad-attach: no input")
        return 1

    name = args.name
    if name is None and not from_stdin:
        name = Path(args.file).name
    if args.mime:
        mime = args.mime
    elif name:
        mime = _guess_mime(name, "text/plain" if from_stdin else "application/octet-stream")
    else:
        mime = sniff_mime(data)

    request: dict[str, Any] = {"cmd": "attach", "kind": kind_for_mime(mime), "mime": mime}
    if name:
        request["filename"] = name

    response = _send(request, data, socket_path=args.socket, timeout=PAYLOAD_TIMEOUT)
    if response is None:
        return 1
    if not response.get("ok"):
        _err(f"scratchpad-attach: {response.get('error', 'request failed')}")
        return 1
    token = response.get("token")
    if not token:
        _err("scratchpad-attach: the instance did not return a token")
        return 1
    print(token)
    return 0


# --------------------------------------------------------------------------- #
# scratchpad-insert
# --------------------------------------------------------------------------- #

def insert_main(argv: list[str] | None = None) -> int:
    """Entry point of ``scratchpad-insert``: insert plain text."""
    parser = argparse.ArgumentParser(
        prog="scratchpad-insert",
        description="Insert TEXT (or stdin) into the scratchpad at the cursor.",
    )
    parser.add_argument("--end", action="store_true", help="append at the end of the document instead")
    parser.add_argument("--socket", metavar="PATH", default=None, help="path of the IPC socket")
    parser.add_argument("text", metavar="TEXT", nargs="*", help="text to insert; read stdin when omitted")
    args = parser.parse_args(argv)

    if args.text:
        text = " ".join(args.text)
    else:
        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    if not text:
        _err("scratchpad-insert: no input")
        return 1

    request: dict[str, Any] = {"cmd": "insert", "where": "end" if args.end else "cursor"}
    payload: bytes | None = None
    encoded = text.encode("utf-8")
    if len(encoded) > INSERT_PAYLOAD_THRESHOLD:
        # The JSON header is capped at ipc.MAX_HEADER_BYTES, so anything big travels
        # as the binary payload (the handler accepts either, see ipc.COMMANDS).
        payload = encoded
    else:
        request["text"] = text
    return _report(_send(request, payload, socket_path=args.socket, timeout=PAYLOAD_TIMEOUT))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
