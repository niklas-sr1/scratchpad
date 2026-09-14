"""IPC between the CLI tools and the running GUI.

Transport: a Unix stream socket at ``<runtime_dir>/ipc.sock``, one request per
connection.

Framing::

    <json object>\n            request header, UTF-8, exactly one line
    <payload_size raw bytes>   present iff the header carries "payload_size"
    <json object>\n            response, written by the server, then it closes

The payload is raw binary (no base64).  Responses are ``{"ok": true, ...}`` or
``{"ok": false, "error": "..."}``.  Commands that carry content (``attach``, and
``insert`` for texts too large for the header) put it in the payload; see
:data:`COMMANDS`.

Serving one connection is bounded in time (:data:`DEFAULT_SERVER_TIMEOUT` overall,
:data:`DEFAULT_IDLE_TIMEOUT` until the client's first byte) so that a slow or
silent peer cannot stall the GTK main thread.

This module must stay free of ``gi``/GLib imports: :class:`IpcServer` only
exposes :meth:`IpcServer.fileno` and :meth:`IpcServer.handle_ready` so that a
GTK main loop can drive it with ``GLib.io_add_watch``.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import select
import socket
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "PROTOCOL_VERSION",
    "SOCKET_NAME",
    "MAX_HEADER_BYTES",
    "MAX_PAYLOAD_BYTES",
    "DEFAULT_CLIENT_TIMEOUT",
    "DEFAULT_SERVER_TIMEOUT",
    "DEFAULT_IDLE_TIMEOUT",
    "STALE_PROBE_TIMEOUT",
    "COMMANDS",
    "IpcError",
    "IpcUnavailable",
    "IpcTimeout",
    "IpcProtocolError",
    "Handler",
    "IpcServer",
    "IpcClient",
    "encode_request",
    "decode_request",
    "read_request",
    "runtime_dir",
    "runtime_socket_path",
    "is_server_alive",
]

#: Bumped when the wire format changes incompatibly.
PROTOCOL_VERSION = 1

#: Socket file name inside the runtime directory.
SOCKET_NAME = "ipc.sock"

#: Hard limit for the JSON header line (1 MiB).
MAX_HEADER_BYTES = 1 << 20

#: Hard limit for a binary payload (512 MiB).
MAX_PAYLOAD_BYTES = 512 << 20

#: Default overall deadline for a client request, in seconds.
DEFAULT_CLIENT_TIMEOUT = 5.0

#: Default *overall* deadline for serving one connection, in seconds.  It is an
#: absolute budget per connection, not a per-read timeout: a client that dribbles
#: single bytes can therefore stall the GTK main thread for at most this long.
DEFAULT_SERVER_TIMEOUT = 10.0

#: How long the server waits for a freshly accepted client to send *anything*
#: before giving up on it, in seconds.  Keeps the common "connect and go away"
#: case far below :data:`DEFAULT_SERVER_TIMEOUT`.
DEFAULT_IDLE_TIMEOUT = 2.0

#: Timeout of the connect() probe that decides whether an existing socket file
#: belongs to a live instance, in seconds.
STALE_PROBE_TIMEOUT = 1.0

#: Every command the protocol defines (see ARCHITECTURE.md section 8).
#:
#: Request fields besides ``cmd`` (ARCHITECTURE.md section 8 is authoritative)::
#:
#:     ping                                  -> {"ok": true, "version": ...}
#:     toggle / show / hide  activation_token?
#:     insert                text or payload (UTF-8), where: "cursor" | "end"
#:     attach                kind, mime?, filename?, payload = content
#:     attach_path           path
#:     screenshot
#:     paste_clipboard       activation_token?
#:     status                                -> visible, chars, events
#:
#: ``insert`` takes its text **either** in the ``text`` header field **or** as the
#: binary payload decoded as UTF-8 (the CLI switches to the payload above
#: ~64 KiB, because the JSON header is capped at :data:`MAX_HEADER_BYTES`).  A
#: handler must therefore treat ``request.get("text")`` and
#: ``payload.decode("utf-8")`` as equivalent sources, in that order.
COMMANDS = (
    "ping",
    "toggle",
    "show",
    "hide",
    "insert",
    "attach",
    "attach_path",
    "screenshot",
    "paste_clipboard",
    "status",
)

#: What the UI supplies to :class:`IpcServer`.
Handler = Callable[[dict[str, Any], "bytes | None"], "dict[str, Any] | None"]

# AF_UNIX paths are limited to sun_path (107 usable bytes on Linux).
_MAX_SOCKET_PATH = 107

_CHUNK = 1 << 16


class IpcError(Exception):
    """Base class for all IPC failures."""


class IpcUnavailable(IpcError):
    """No server is listening on the socket (absent, stale or refused)."""


class IpcTimeout(IpcError):
    """The peer did not complete the exchange within the deadline."""


class IpcProtocolError(IpcError):
    """The peer sent something that is not a well-formed message."""


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

def runtime_dir() -> Path:
    """Return (and create, mode 0700) the runtime directory.

    ``$XDG_RUNTIME_DIR/scratchpad`` when that variable is set and usable,
    otherwise ``<tmp>/scratchpad-<uid>``.

    Implemented locally on purpose: ``scratchpad.paths`` belongs to another
    module and ``ipc`` must stay dependency-free.
    """
    candidates: list[Path] = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(Path(xdg) / "scratchpad")
    tmp = os.environ.get("TMPDIR") or tempfile.gettempdir()
    candidates.append(Path(tmp) / f"scratchpad-{os.getuid()}")

    last_error: OSError | None = None
    for directory in candidates:
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - depends on the environment
            last_error = exc
            continue
        try:
            os.chmod(directory, 0o700)
        except OSError:  # pragma: no cover - e.g. not the owner
            pass
        return directory
    assert last_error is not None  # pragma: no cover
    raise IpcError(f"cannot create a runtime directory: {last_error}")


def runtime_socket_path() -> Path:
    """Path of the IPC socket."""
    return runtime_dir() / SOCKET_NAME


def _check_socket_path(path: Path) -> str:
    text = str(path)
    if len(os.fsencode(text)) > _MAX_SOCKET_PATH:
        raise IpcError(
            f"socket path is too long for AF_UNIX ({len(text)} > {_MAX_SOCKET_PATH}): {text}"
        )
    return text


# --------------------------------------------------------------------------- #
# wire format
# --------------------------------------------------------------------------- #

def encode_request(request: dict[str, Any], payload: bytes | None = None) -> bytes:
    """Serialise ``request`` (plus optional binary ``payload``) to wire bytes."""
    header = dict(request)
    if payload is None:
        header.pop("payload_size", None)
    else:
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise IpcProtocolError(
                f"payload of {len(payload)} bytes exceeds the limit of {MAX_PAYLOAD_BYTES}"
            )
        header["payload_size"] = len(payload)
    line = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if b"\n" in line:  # pragma: no cover - json.dumps escapes newlines
        raise IpcProtocolError("encoded header contains a newline")
    if len(line) + 1 > MAX_HEADER_BYTES:
        raise IpcProtocolError(
            f"header of {len(line)} bytes exceeds the limit of {MAX_HEADER_BYTES}"
        )
    if payload is None:
        return line + b"\n"
    return line + b"\n" + payload


def encode_response(response: dict[str, Any]) -> bytes:
    """Serialise a response object to one JSON line."""
    return json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _parse_header(line: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IpcProtocolError(f"malformed request header: {exc}") from exc
    if not isinstance(obj, dict):
        raise IpcProtocolError("request header is not a JSON object")
    return obj


def _payload_size(header: dict[str, Any]) -> int | None:
    if "payload_size" not in header:
        return None
    size = header["payload_size"]
    if isinstance(size, bool) or not isinstance(size, int):
        raise IpcProtocolError("payload_size must be an integer")
    if size < 0:
        raise IpcProtocolError("payload_size must not be negative")
    if size > MAX_PAYLOAD_BYTES:
        raise IpcProtocolError(
            f"payload_size {size} exceeds the limit of {MAX_PAYLOAD_BYTES} bytes"
        )
    return size


def decode_request(data: bytes) -> tuple[dict[str, Any], bytes | None]:
    """Inverse of :func:`encode_request` for a complete byte string."""
    newline = data.find(b"\n")
    if newline < 0:
        raise IpcProtocolError("request header is not newline terminated")
    if newline + 1 > MAX_HEADER_BYTES:
        raise IpcProtocolError("request header exceeds the size limit")
    header = _parse_header(data[:newline])
    size = _payload_size(header)
    if size is None:
        return header, None
    header.pop("payload_size")
    body = data[newline + 1 :]
    if len(body) != size:
        raise IpcProtocolError(
            f"payload is {len(body)} bytes but payload_size says {size}"
        )
    return header, body


class _SocketReader:
    """Buffered reader over a blocking socket with a byte budget.

    ``deadline`` is an absolute :func:`time.monotonic` instant.  When it is set
    the socket timeout is recomputed from the time left before *every* read, so
    the total time spent on one peer is bounded no matter how it paces its
    writes.
    """

    def __init__(self, sock: socket.socket, deadline: float | None = None) -> None:
        self._sock = sock
        self._buf = bytearray()
        self._deadline = deadline

    def _recv(self) -> bytes:
        if self._deadline is not None:
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise IpcTimeout("timed out while reading from the peer")
            self._sock.settimeout(left)
        try:
            return self._sock.recv(_CHUNK)
        except TimeoutError as exc:
            raise IpcTimeout("timed out while reading from the peer") from exc
        except OSError as exc:
            raise IpcError(f"read failed: {exc}") from exc

    def read_line(self, limit: int) -> bytes:
        """Read up to and including the next newline; return it without the newline."""
        while True:
            index = self._buf.find(b"\n")
            if index >= 0:
                line = bytes(self._buf[:index])
                del self._buf[: index + 1]
                return line
            if len(self._buf) >= limit:
                raise IpcProtocolError(f"header exceeds the limit of {limit} bytes")
            chunk = self._recv()
            if not chunk:
                if not self._buf:
                    raise IpcProtocolError("peer closed the connection before sending a request")
                raise IpcProtocolError("peer closed the connection inside the request header")
            self._buf.extend(chunk)

    def read_exactly(self, size: int) -> bytes:
        if size <= len(self._buf):
            data = bytes(self._buf[:size])
            del self._buf[:size]
            return data
        parts = [bytes(self._buf)]
        missing = size - len(self._buf)
        self._buf.clear()
        while missing > 0:
            chunk = self._recv()
            if not chunk:
                raise IpcProtocolError(
                    f"peer closed the connection with {missing} payload bytes missing"
                )
            if len(chunk) > missing:
                parts.append(chunk[:missing])
                self._buf.extend(chunk[missing:])
                missing = 0
            else:
                parts.append(chunk)
                missing -= len(chunk)
        return b"".join(parts)


def read_request(
    sock: socket.socket, *, deadline: float | None = None
) -> tuple[dict[str, Any], bytes | None]:
    """Read exactly one request from a connected socket.

    Returns ``(request, payload)``; ``payload`` is ``None`` when the request
    carried none.  The ``payload_size`` framing key is removed from the returned
    dict.  Raises :class:`IpcProtocolError` on malformed input (including
    oversized headers or payloads) and :class:`IpcTimeout` on a stalled peer.

    ``deadline`` is an absolute :func:`time.monotonic` instant bounding the whole
    read; without it only the socket's own timeout applies (per read).
    """
    reader = _SocketReader(sock, deadline)
    line = reader.read_line(MAX_HEADER_BYTES)
    header = _parse_header(line)
    size = _payload_size(header)
    if size is None:
        return header, None
    header.pop("payload_size")
    return header, reader.read_exactly(size)


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #

def _socket_is_live(path: Path, *, timeout: float = STALE_PROBE_TIMEOUT) -> bool:
    """True when something accepts connections on ``path``.

    Deliberately *not* a ``ping``: an instance that is busy (inside a modal
    dialog, say) does not answer in time yet must never have its socket removed
    from under it.  Only ``ECONNREFUSED``/``ENOENT`` - nobody is listening -
    make a socket file stale; everything else is treated as "in use".
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(timeout)
        probe.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
            return False
        log.debug("stale check for %s is inconclusive (%s); assuming it is in use", path, exc)
        return True
    finally:
        probe.close()
    return True


class IpcServer:
    """Accepting side of the protocol, free of any GLib dependency.

    The owner is expected to watch :meth:`fileno` for readability and call
    :meth:`handle_ready` from its main loop; each call accepts at most one
    pending connection, serves it synchronously and closes it.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike[str],
        handler: Handler,
        *,
        backlog: int = 16,
        read_timeout: float = DEFAULT_SERVER_TIMEOUT,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self.path = Path(socket_path)
        self.handler = handler
        #: Overall budget for one connection (accept to response), in seconds.
        self.read_timeout = read_timeout
        #: How long a freshly accepted client may stay silent, in seconds.
        self.idle_timeout = idle_timeout
        text = _check_socket_path(self.path)

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_socket():
            if _socket_is_live(self.path):
                raise IpcError(f"another scratchpad instance is listening on {text}")
            log.info("removing stale socket %s", text)
            try:
                self.path.unlink()
            except FileNotFoundError:  # pragma: no cover - racy
                pass

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old_mask = os.umask(0o177)
            try:
                self._sock.bind(text)
            finally:
                os.umask(old_mask)
            os.chmod(text, 0o600)
            self._sock.listen(backlog)
            self._sock.setblocking(False)
        except BaseException:
            self._sock.close()
            raise
        self._closed = False
        log.debug("ipc server listening on %s", text)

    # -- main loop integration ------------------------------------------- #

    def fileno(self) -> int:
        """File descriptor of the listening socket (for ``GLib.io_add_watch``)."""
        return self._sock.fileno()

    def handle_ready(self) -> bool:
        """Accept and serve exactly one pending connection.

        Always returns ``True`` so it can be used directly as a GLib watch
        callback (the watch stays installed).  Never raises: every failure is
        logged and, where possible, reported to the client.  The call blocks for
        at most :attr:`read_timeout` seconds in total (:attr:`idle_timeout` when
        the client never sends anything) plus the handler's own runtime.
        """
        if self._closed:
            return False
        try:
            conn, _ = self._sock.accept()
        except (BlockingIOError, InterruptedError):
            return True
        except OSError as exc:  # pragma: no cover - listener died
            log.warning("ipc accept failed: %s", exc)
            return True
        try:
            self._serve(conn)
        finally:
            try:
                conn.close()
            except OSError:  # pragma: no cover
                pass
        return True

    def _wait_readable(self, conn: socket.socket, deadline: float | None) -> bool:
        """Wait (briefly) for the client's first bytes.  False when it stays silent."""
        budget = self.idle_timeout if self.idle_timeout and self.idle_timeout > 0 else None
        if deadline is not None:
            left = max(0.0, deadline - time.monotonic())
            budget = left if budget is None else min(budget, left)
        if budget is None:
            return True
        try:
            readable, _, _ = select.select([conn], [], [], budget)
        except (OSError, ValueError) as exc:  # pragma: no cover - fd went away
            log.warning("ipc select failed: %s", exc)
            return True
        return bool(readable)

    def _serve(self, conn: socket.socket) -> None:
        conn.setblocking(True)
        deadline = time.monotonic() + self.read_timeout if self.read_timeout else None
        conn.settimeout(self.read_timeout or None)
        response: dict[str, Any]
        if not self._wait_readable(conn, deadline):
            limits = [t for t in (self.idle_timeout, self.read_timeout) if t and t > 0]
            exc = IpcTimeout(f"client sent nothing within {min(limits):g}s")
            log.warning("ipc request rejected: %s", exc)
            self._write(conn, {"ok": False, "error": str(exc)})
            return
        try:
            request, payload = read_request(conn, deadline=deadline)
        except IpcError as exc:
            log.warning("ipc request rejected: %s", exc)
            self._write(conn, {"ok": False, "error": str(exc)})
            return
        try:
            result = self.handler(request, payload)
        except Exception as exc:  # noqa: BLE001 - never let the UI loop die
            log.exception("ipc handler failed for %r", request.get("cmd"))
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            if result is None:
                response = {"ok": True}
            elif isinstance(result, dict):
                response = dict(result)
                response.setdefault("ok", True)
            else:  # pragma: no cover - handler contract violation
                response = {"ok": False, "error": "handler returned a non-object"}
        self._write(conn, response)

    def _write(self, conn: socket.socket, response: dict[str, Any]) -> None:
        try:
            data = encode_response(response)
        except (TypeError, ValueError) as exc:  # pragma: no cover - handler bug
            log.exception("cannot serialise ipc response")
            data = encode_response({"ok": False, "error": f"unserialisable response: {exc}"})
        try:
            # The read may have consumed the whole budget; give the write its own.
            conn.settimeout(self.read_timeout or None)
            conn.sendall(data)
            conn.shutdown(socket.SHUT_WR)
        except OSError as exc:
            log.warning("ipc response could not be delivered: %s", exc)

    # -- lifetime --------------------------------------------------------- #

    def close(self) -> None:
        """Stop listening and remove the socket file.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:  # pragma: no cover
            pass
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover
            log.warning("could not remove %s: %s", self.path, exc)

    def __enter__(self) -> IpcServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #

class IpcClient:
    """Connecting side of the protocol.  One connection per request."""

    def __init__(self, socket_path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(socket_path) if socket_path is not None else runtime_socket_path()

    def request(
        self,
        request: dict[str, Any],
        payload: bytes | None = None,
        timeout: float = DEFAULT_CLIENT_TIMEOUT,
    ) -> dict[str, Any]:
        """Send one request and return the decoded response object.

        ``timeout`` is an overall deadline in seconds for connect, send and
        receive together; ``0`` or ``None`` blocks indefinitely.  Raises
        :class:`IpcUnavailable` when nothing is listening, :class:`IpcTimeout`
        when the deadline passes and :class:`IpcProtocolError` on a malformed
        reply.
        """
        data = encode_request(request, payload)
        text = _check_socket_path(self.path)
        deadline = time.monotonic() + timeout if timeout else None

        def remaining() -> float | None:
            if deadline is None:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                raise IpcTimeout(f"timed out after {timeout:g}s talking to {text}")
            return left

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(remaining())
            try:
                sock.connect(text)
            except TimeoutError as exc:
                raise IpcTimeout(f"timed out connecting to {text}") from exc
            except OSError as exc:
                if exc.errno in (
                    errno.ENOENT,
                    errno.ECONNREFUSED,
                    errno.ECONNRESET,
                    errno.EACCES,
                    errno.ENOTDIR,
                ):
                    raise IpcUnavailable(f"no scratchpad instance at {text}: {exc.strerror}") from exc
                raise IpcError(f"cannot connect to {text}: {exc}") from exc
            try:
                sock.settimeout(remaining())
                sock.sendall(data)
                sock.shutdown(socket.SHUT_WR)
            except TimeoutError as exc:
                raise IpcTimeout(f"timed out sending the request to {text}") from exc
            except BrokenPipeError as exc:
                raise IpcUnavailable(f"scratchpad closed the connection at {text}") from exc
            except OSError as exc:
                raise IpcError(f"cannot send the request to {text}: {exc}") from exc

            sock.settimeout(remaining())
            reader = _SocketReader(sock)
            try:
                line = reader.read_line(MAX_HEADER_BYTES)
            except IpcProtocolError as exc:
                raise IpcProtocolError(f"no usable response from {text}: {exc}") from exc
        finally:
            sock.close()

        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IpcProtocolError(f"malformed response from {text}: {exc}") from exc
        if not isinstance(response, dict):
            raise IpcProtocolError(f"response from {text} is not a JSON object")
        return response


def is_server_alive(path: str | os.PathLike[str] | None = None, *, timeout: float = 1.5) -> bool:
    """True when a server answers ``ping`` on ``path`` (default: the runtime socket)."""
    try:
        response = IpcClient(path).request({"cmd": "ping"}, timeout=timeout)
    except IpcError:
        return False
    return bool(response.get("ok"))
