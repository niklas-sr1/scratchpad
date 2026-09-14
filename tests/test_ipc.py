"""Tests for the CLI/GUI IPC protocol (scratchpad.ipc)."""

from __future__ import annotations

import json
import os
import select
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from scratchpad import ipc
from scratchpad.ipc import (
    IpcClient,
    IpcProtocolError,
    IpcServer,
    IpcTimeout,
    IpcUnavailable,
    decode_request,
    encode_request,
    is_server_alive,
    read_request,
    runtime_dir,
    runtime_socket_path,
)


# --------------------------------------------------------------------------- #
# a GLib-free stub server driven by select(), like the GTK main loop would
# --------------------------------------------------------------------------- #

class StubServer:
    """Runs an :class:`IpcServer` in a thread with a plain ``select`` loop."""

    def __init__(self, path: Path, handler) -> None:
        self.requests: list[tuple[dict[str, Any], bytes | None]] = []
        self._handler = handler
        self.server = IpcServer(path, self._record)
        self._stop_r, self._stop_w = os.pipe()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _record(self, request: dict[str, Any], payload: bytes | None) -> dict[str, Any] | None:
        self.requests.append((request, payload))
        return self._handler(request, payload)

    def _loop(self) -> None:
        while True:
            readable, _, _ = select.select([self.server.fileno(), self._stop_r], [], [])
            if self._stop_r in readable:
                return
            if self.server.fileno() in readable:
                self.server.handle_ready()

    def stop(self) -> None:
        os.write(self._stop_w, b"x")
        self._thread.join(timeout=5)
        self.server.close()
        os.close(self._stop_r)
        os.close(self._stop_w)


def default_handler(request: dict[str, Any], payload: bytes | None) -> dict[str, Any]:
    """Stub of the handler the UI agent must provide."""
    cmd = request.get("cmd")
    if cmd == "ping":
        return {"ok": True, "version": "0.1.0"}
    if cmd == "status":
        return {"ok": True, "visible": True, "chars": 12, "events": 34}
    if cmd == "attach":
        return {"ok": True, "token": "0" * 22, "object_id": 7, "size": len(payload or b"")}
    if cmd in ("toggle", "show", "hide", "insert", "attach_path", "screenshot", "paste_clipboard"):
        return {"ok": True}
    return {"ok": False, "error": f"unknown command: {cmd!r}"}


@pytest.fixture
def sock_path(tmp_path: Path) -> Path:
    return tmp_path / "s.sock"


@pytest.fixture
def server(sock_path: Path):
    stub = StubServer(sock_path, default_handler)
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture
def client(sock_path: Path) -> IpcClient:
    return IpcClient(sock_path)


# --------------------------------------------------------------------------- #
# wire format
# --------------------------------------------------------------------------- #

def test_encode_decode_round_trip_without_payload() -> None:
    request = {"cmd": "insert", "text": "hällo\nworld", "where": "end"}
    wire = encode_request(request, None)
    assert wire.endswith(b"\n")
    assert wire.count(b"\n") == 1
    assert decode_request(wire) == (request, None)


def test_encode_sets_payload_size_and_decode_strips_it() -> None:
    wire = encode_request({"cmd": "attach", "kind": "text"}, b"abc")
    header, payload = wire.split(b"\n", 1)
    assert json.loads(header)["payload_size"] == 3
    assert payload == b"abc"
    request, decoded = decode_request(wire)
    assert request == {"cmd": "attach", "kind": "text"}
    assert "payload_size" not in request
    assert decoded == b"abc"


def test_encode_ignores_stale_payload_size_when_no_payload() -> None:
    request, payload = decode_request(encode_request({"cmd": "ping", "payload_size": 9}, None))
    assert request == {"cmd": "ping"}
    assert payload is None


def test_empty_payload_is_preserved() -> None:
    assert decode_request(encode_request({"cmd": "attach"}, b"")) == ({"cmd": "attach"}, b"")


def test_read_request_over_a_socket_pair_with_5mb_payload() -> None:
    blob = os.urandom(5 * 1024 * 1024)
    request = {"cmd": "attach", "kind": "file", "mime": "application/octet-stream"}
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        wire = encode_request(request, blob)

        def send() -> None:
            left.sendall(wire)
            left.shutdown(socket.SHUT_WR)

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        right.settimeout(20)
        got_request, got_payload = read_request(right)
        thread.join(timeout=20)
    finally:
        left.close()
        right.close()
    assert got_request == request
    assert got_payload == blob


def test_read_request_rejects_a_truncated_payload() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        left.sendall(encode_request({"cmd": "attach"}, b"0123456789")[:-4])
        left.shutdown(socket.SHUT_WR)
        right.settimeout(5)
        with pytest.raises(IpcProtocolError, match="payload bytes missing"):
            read_request(right)
    finally:
        left.close()
        right.close()


def test_read_request_rejects_a_giant_header() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        right.settimeout(5)
        junk = b"x" * (1 << 16)

        def flood() -> None:
            try:
                for _ in range((ipc.MAX_HEADER_BYTES // len(junk)) + 4):
                    left.sendall(junk)
            except OSError:
                pass

        thread = threading.Thread(target=flood, daemon=True)
        thread.start()
        with pytest.raises(IpcProtocolError, match="limit"):
            read_request(right)
    finally:
        left.close()
        right.close()


def test_encode_request_rejects_an_oversized_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "MAX_PAYLOAD_BYTES", 8)
    with pytest.raises(IpcProtocolError, match="exceeds the limit"):
        encode_request({"cmd": "attach"}, b"123456789")


def test_declared_payload_size_over_the_limit_is_rejected() -> None:
    wire = b'{"cmd":"attach","payload_size":%d}\n' % (ipc.MAX_PAYLOAD_BYTES + 1)
    with pytest.raises(IpcProtocolError, match="exceeds the limit"):
        decode_request(wire)


def test_malformed_header_is_rejected() -> None:
    with pytest.raises(IpcProtocolError):
        decode_request(b"not json\n")
    with pytest.raises(IpcProtocolError, match="not a JSON object"):
        decode_request(b"[1, 2]\n")
    with pytest.raises(IpcProtocolError, match="newline"):
        decode_request(b'{"cmd":"ping"}')


# --------------------------------------------------------------------------- #
# runtime paths
# --------------------------------------------------------------------------- #

def test_runtime_dir_uses_xdg_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    directory = runtime_dir()
    assert directory == tmp_path / "scratchpad"
    assert directory.is_dir()
    assert oct(directory.stat().st_mode & 0o777) == "0o700"
    assert runtime_socket_path() == directory / "ipc.sock"


def test_runtime_dir_falls_back_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    directory = runtime_dir()
    assert directory == tmp_path / f"scratchpad-{os.getuid()}"
    assert directory.is_dir()


# --------------------------------------------------------------------------- #
# server / client
# --------------------------------------------------------------------------- #

def test_ping_and_is_server_alive(server: StubServer, client: IpcClient, sock_path: Path) -> None:
    assert client.request({"cmd": "ping"}) == {"ok": True, "version": "0.1.0"}
    assert is_server_alive(sock_path) is True


@pytest.mark.parametrize(
    "request_obj",
    [
        {"cmd": "ping"},
        {"cmd": "toggle", "activation_token": "tok-1"},
        {"cmd": "show", "activation_token": "tok-2"},
        {"cmd": "hide"},
        {"cmd": "insert", "text": "hello", "where": "cursor"},
        {"cmd": "insert", "text": "hello", "where": "end"},
        {"cmd": "attach_path", "path": "/tmp/x.png"},
        {"cmd": "screenshot"},
        {"cmd": "paste_clipboard", "activation_token": "tok-3"},
        {"cmd": "status"},
    ],
)
def test_every_command_round_trips(server: StubServer, client: IpcClient, request_obj: dict) -> None:
    response = client.request(dict(request_obj))
    assert response["ok"] is True
    assert server.requests[-1][0] == request_obj
    assert server.requests[-1][1] is None


def test_attach_command_carries_a_binary_payload(server: StubServer, client: IpcClient) -> None:
    blob = os.urandom(5 * 1024 * 1024)
    response = client.request(
        {"cmd": "attach", "kind": "file", "mime": "application/octet-stream", "filename": "b.bin"},
        blob,
        timeout=30.0,
    )
    assert response["ok"] is True
    assert response["size"] == len(blob)
    assert response["token"] == "0" * 22
    assert server.requests[-1][1] == blob


def test_requests_are_served_one_after_another(server: StubServer, client: IpcClient) -> None:
    for index in range(5):
        assert client.request({"cmd": "insert", "text": str(index), "where": "cursor"})["ok"]
    assert len(server.requests) == 5


def test_unknown_command_produces_an_error_response(server: StubServer, client: IpcClient) -> None:
    response = client.request({"cmd": "nonsense"})
    assert response["ok"] is False
    assert "nonsense" in response["error"]


def test_handler_exception_becomes_an_error_response(sock_path: Path) -> None:
    def boom(request: dict, payload: bytes | None) -> dict:
        raise RuntimeError("handler exploded")

    stub = StubServer(sock_path, boom)
    try:
        response = IpcClient(sock_path).request({"cmd": "toggle"})
    finally:
        stub.stop()
    assert response == {"ok": False, "error": "RuntimeError: handler exploded"}


def test_handler_returning_none_means_ok(sock_path: Path) -> None:
    stub = StubServer(sock_path, lambda request, payload: None)
    try:
        assert IpcClient(sock_path).request({"cmd": "hide"}) == {"ok": True}
    finally:
        stub.stop()


def test_handler_dict_without_ok_defaults_to_ok(sock_path: Path) -> None:
    stub = StubServer(sock_path, lambda request, payload: {"token": "abc"})
    try:
        assert IpcClient(sock_path).request({"cmd": "attach"}) == {"ok": True, "token": "abc"}
    finally:
        stub.stop()


def test_client_raises_ipc_unavailable_without_a_server(tmp_path: Path) -> None:
    missing = tmp_path / "nothing.sock"
    with pytest.raises(IpcUnavailable):
        IpcClient(missing).request({"cmd": "ping"})
    assert is_server_alive(missing) is False


def test_client_raises_ipc_unavailable_on_a_stale_socket(tmp_path: Path) -> None:
    stale = tmp_path / "stale.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(stale))
    listener.close()
    assert stale.exists()
    with pytest.raises(IpcUnavailable):
        IpcClient(stale).request({"cmd": "ping"})


def test_server_replaces_a_stale_socket_file(tmp_path: Path) -> None:
    stale = tmp_path / "stale2.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(stale))
    listener.close()
    stub = StubServer(stale, default_handler)
    try:
        assert IpcClient(stale).request({"cmd": "ping"})["ok"] is True
    finally:
        stub.stop()
    assert not stale.exists()


def test_second_server_on_a_live_socket_is_refused(server: StubServer, sock_path: Path) -> None:
    with pytest.raises(ipc.IpcError, match="another scratchpad instance"):
        IpcServer(sock_path, default_handler)
    assert sock_path.is_socket()  # the live instance keeps its socket
    assert IpcClient(sock_path).request({"cmd": "ping"})["ok"] is True


def test_a_busy_instance_that_cannot_answer_ping_keeps_its_socket(tmp_path: Path) -> None:
    """A live server inside a modal dialog never answers ping; it is not stale."""
    busy = tmp_path / "busy.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(busy))
    listener.listen(4)  # listening, but nothing will ever accept or reply
    try:
        assert is_server_alive(busy, timeout=0.3) is False  # ping says nothing answers
        started = time.monotonic()
        with pytest.raises(ipc.IpcError, match="another scratchpad instance"):
            IpcServer(busy, default_handler)
        assert time.monotonic() - started < 2.0
        assert busy.is_socket()
        # The busy instance can still be reached afterwards.
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(2)
        probe.connect(str(busy))
        probe.close()
    finally:
        listener.close()


def test_socket_is_live_distinguishes_stale_from_listening(tmp_path: Path) -> None:
    missing = tmp_path / "absent.sock"
    assert ipc._socket_is_live(missing) is False

    stale = tmp_path / "leftover.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(stale))
    listener.close()
    assert stale.exists()
    assert ipc._socket_is_live(stale) is False

    live = tmp_path / "live.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(live))
    listener.listen(1)
    try:
        assert ipc._socket_is_live(live) is True
    finally:
        listener.close()


def test_close_removes_the_socket_file(sock_path: Path) -> None:
    stub = StubServer(sock_path, default_handler)
    assert sock_path.exists()
    stub.stop()
    assert not sock_path.exists()
    stub.server.close()  # idempotent


def test_client_timeout_when_the_handler_is_slow(sock_path: Path) -> None:
    def slow(request: dict, payload: bytes | None) -> dict:
        time.sleep(1.0)
        return {"ok": True}

    stub = StubServer(sock_path, slow)
    try:
        started = time.monotonic()
        with pytest.raises(IpcTimeout):
            IpcClient(sock_path).request({"cmd": "toggle"}, timeout=0.2)
        assert time.monotonic() - started < 0.9
    finally:
        stub.stop()


def test_server_read_timeout_protects_against_a_stuck_client(sock_path: Path) -> None:
    calls: list[tuple] = []
    server = IpcServer(sock_path, lambda request, payload: calls.append((request, payload)) or {"ok": True},
                       read_timeout=0.2)
    try:
        stuck = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stuck.connect(str(sock_path))
        stuck.sendall(b'{"cmd": "pi')  # no newline, then nothing at all
        started = time.monotonic()
        assert server.handle_ready() is True
        elapsed = time.monotonic() - started
        assert 0.1 < elapsed < 2.0
        assert calls == []
        stuck.settimeout(2)
        response = json.loads(stuck.recv(4096).decode())
        assert response["ok"] is False
        stuck.close()
    finally:
        server.close()


def test_server_bounds_a_dribbling_client_by_an_overall_deadline(sock_path: Path) -> None:
    """One byte every (timeout - epsilon) must not keep the main thread forever."""
    calls: list[tuple] = []
    server = IpcServer(
        sock_path,
        lambda request, payload: calls.append((request, payload)) or {"ok": True},
        read_timeout=1.0,
        idle_timeout=1.0,
    )
    stop = threading.Event()

    def dribble(sock: socket.socket) -> None:
        # Always faster than the per-read timeout, so only an absolute deadline
        # can stop this client.
        for byte in b'{"cmd": "ping", "pad": "' + b"x" * 200:
            if stop.is_set():
                return
            try:
                sock.sendall(bytes([byte]))
            except OSError:
                return
            time.sleep(0.3)

    try:
        slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        slow.connect(str(sock_path))
        thread = threading.Thread(target=dribble, args=(slow,), daemon=True)
        thread.start()
        started = time.monotonic()
        assert server.handle_ready() is True
        elapsed = time.monotonic() - started
        assert 0.9 < elapsed < 3.0, elapsed
        assert calls == []
        slow.settimeout(2)
        response = json.loads(slow.recv(4096).decode())
        assert response["ok"] is False
        assert "timed out" in response["error"]
    finally:
        stop.set()
        slow.close()
        server.close()


def test_server_gives_up_on_a_silent_client_after_the_idle_timeout(sock_path: Path) -> None:
    """A client that connects and sends nothing costs the idle timeout, not the full one."""
    server = IpcServer(sock_path, default_handler, read_timeout=30.0)
    assert server.idle_timeout == ipc.DEFAULT_IDLE_TIMEOUT == 2.0
    try:
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        silent.connect(str(sock_path))
        started = time.monotonic()
        assert server.handle_ready() is True
        elapsed = time.monotonic() - started
        assert elapsed < server.idle_timeout + 1.0, elapsed
        silent.settimeout(2)
        response = json.loads(silent.recv(4096).decode())
        assert response["ok"] is False
        assert "sent nothing" in response["error"]
        silent.close()
    finally:
        server.close()


def test_server_still_reads_a_large_payload_that_keeps_flowing(sock_path: Path) -> None:
    """The deadline must not break legitimate multi-megabyte transfers."""
    seen: list[bytes | None] = []
    server = IpcServer(
        sock_path,
        lambda request, payload: seen.append(payload) or {"ok": True},
        read_timeout=30.0,
    )
    blob = os.urandom(4 * 1024 * 1024)
    try:
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sender.connect(str(sock_path))
        wire = encode_request({"cmd": "attach", "kind": "file"}, blob)

        def send() -> None:
            sender.sendall(wire)

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        assert server.handle_ready() is True
        thread.join(timeout=30)
        assert seen == [blob]
        sender.close()
    finally:
        server.close()


def test_handle_ready_returns_true_without_a_pending_connection(sock_path: Path) -> None:
    server = IpcServer(sock_path, default_handler)
    try:
        assert server.handle_ready() is True
    finally:
        server.close()


def test_fileno_is_selectable(sock_path: Path) -> None:
    server = IpcServer(sock_path, default_handler)
    try:
        assert select.select([server.fileno()], [], [], 0) == ([], [], [])
        client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client_sock.connect(str(sock_path))
        readable, _, _ = select.select([server.fileno()], [], [], 2)
        assert readable == [server.fileno()]
        client_sock.close()
    finally:
        server.close()


def test_socket_path_that_is_too_long_is_rejected(tmp_path: Path) -> None:
    long_path = tmp_path / ("x" * 120)
    with pytest.raises(ipc.IpcError, match="too long"):
        IpcClient(long_path).request({"cmd": "ping"})
