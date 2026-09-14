"""Tests for the command line front ends (scratchpad.cli)."""

from __future__ import annotations

import copy
import io
import os
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scratchpad import cli
from scratchpad.ipc import DEFAULT_CLIENT_TIMEOUT, IpcClient, IpcUnavailable

from tests.test_ipc import StubServer, default_handler  # reuse the threaded stub server

#: Captured before any fixture replaces it.
REAL_SPAWN_GUI = cli._spawn_gui


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def no_accidental_gui(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test must never really spawn the GUI."""

    def forbidden() -> None:
        raise AssertionError("the CLI tried to spawn the GUI")

    monkeypatch.setattr(cli, "_spawn_gui", forbidden)
    monkeypatch.delenv("XDG_ACTIVATION_TOKEN", raising=False)


@pytest.fixture
def ipc_stub(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Record every IpcClient.request call and return a canned response."""
    state = SimpleNamespace(calls=[], response={"ok": True}, error=None)

    def request(
        self: IpcClient,
        request_obj: dict[str, Any],
        payload: bytes | None = None,
        timeout: float = DEFAULT_CLIENT_TIMEOUT,
    ) -> dict[str, Any]:
        state.calls.append(
            SimpleNamespace(
                request=copy.deepcopy(request_obj),
                payload=payload,
                timeout=timeout,
                path=Path(self.path),
            )
        )
        if state.error is not None:
            raise state.error
        return state.response

    monkeypatch.setattr(IpcClient, "request", request)
    return state


def set_stdin(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    stream = SimpleNamespace(buffer=io.BytesIO(data))
    monkeypatch.setattr(sys, "stdin", stream)


# --------------------------------------------------------------------------- #
# scratchpad: request shapes
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], {"cmd": "toggle"}),
        (["toggle"], {"cmd": "toggle"}),
        (["show"], {"cmd": "show"}),
        (["hide"], {"cmd": "hide"}),
        (["screenshot"], {"cmd": "screenshot"}),
        (["paste-clipboard"], {"cmd": "paste_clipboard"}),
        (["status"], {"cmd": "status"}),
    ],
)
def test_subcommands_send_the_expected_request(
    ipc_stub: SimpleNamespace, argv: list[str], expected: dict
) -> None:
    assert cli.main(argv) == 0
    assert len(ipc_stub.calls) == 1
    assert ipc_stub.calls[0].request == expected
    assert ipc_stub.calls[0].payload is None


def test_socket_option_is_honoured(ipc_stub: SimpleNamespace, tmp_path: Path) -> None:
    custom = tmp_path / "custom.sock"
    assert cli.main(["--socket", str(custom), "show"]) == 0
    assert ipc_stub.calls[0].path == custom


def test_default_socket_comes_from_xdg_runtime_dir(
    ipc_stub: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert cli.main(["show"]) == 0
    assert ipc_stub.calls[0].path == tmp_path / "scratchpad" / "ipc.sock"


def test_error_response_exits_nonzero(ipc_stub: SimpleNamespace, capsys: pytest.CaptureFixture) -> None:
    ipc_stub.response = {"ok": False, "error": "nope"}
    assert cli.main(["show"]) == 1
    assert "nope" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# activation token forwarding
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("argv", "cmd"),
    [([], "toggle"), (["toggle"], "toggle"), (["show"], "show"), (["hide"], "hide"),
     (["paste-clipboard"], "paste_clipboard")],
)
def test_activation_token_is_forwarded(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, argv: list[str], cmd: str
) -> None:
    monkeypatch.setenv("XDG_ACTIVATION_TOKEN", "wayland-token-42")
    assert cli.main(argv) == 0
    assert ipc_stub.calls[0].request == {"cmd": cmd, "activation_token": "wayland-token-42"}


def test_no_activation_token_key_when_the_environment_has_none(ipc_stub: SimpleNamespace) -> None:
    assert cli.main(["toggle"]) == 0
    assert "activation_token" not in ipc_stub.calls[0].request


def test_empty_activation_token_is_not_forwarded(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_ACTIVATION_TOKEN", "")
    assert cli.main(["toggle"]) == 0
    assert ipc_stub.calls[0].request == {"cmd": "toggle"}


def test_activation_token_is_not_sent_with_data_commands(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_ACTIVATION_TOKEN", "wayland-token-42")
    assert cli.insert_main(["hi"]) == 0
    assert ipc_stub.calls[0].request == {"cmd": "insert", "text": "hi", "where": "cursor"}


# --------------------------------------------------------------------------- #
# not running / autostart
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("argv", [["hide"], ["status"]])
def test_hide_and_status_do_not_start_the_gui(
    ipc_stub: SimpleNamespace, capsys: pytest.CaptureFixture, argv: list[str]
) -> None:
    ipc_stub.error = IpcUnavailable("no socket")
    assert cli.main(argv) == 1  # the autouse fixture makes a spawn attempt fail loudly
    assert "not running" in capsys.readouterr().err


def test_toggle_starts_the_gui_and_does_not_resend(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ipc_stub.error = IpcUnavailable("no socket")
    spawned: list[bool] = []
    monkeypatch.setattr(cli, "_spawn_gui", lambda: spawned.append(True) or SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(cli, "is_server_alive", lambda path, timeout=1.0: True)
    assert cli.main(["toggle"]) == 0
    assert spawned == [True]
    assert len(ipc_stub.calls) == 1  # the toggle is not replayed onto the fresh window


def test_show_starts_the_gui_and_resends(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def request(self, request_obj, payload=None, timeout=DEFAULT_CLIENT_TIMEOUT):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IpcUnavailable("no socket")
        return {"ok": True}

    monkeypatch.setattr(IpcClient, "request", request)
    monkeypatch.setattr(cli, "_spawn_gui", lambda: SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(cli, "is_server_alive", lambda path, timeout=1.0: True)
    assert cli.main(["show"]) == 0
    assert calls["n"] == 2


def test_gui_that_never_answers_reports_failure(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    ipc_stub.error = IpcUnavailable("no socket")
    monkeypatch.setattr(cli, "_spawn_gui", lambda: SimpleNamespace(poll=lambda: 1))
    monkeypatch.setattr(cli, "is_server_alive", lambda path, timeout=1.0: False)
    monkeypatch.setattr(cli, "START_TIMEOUT", 0.3)
    assert cli.main(["show"]) == 1
    assert "did not come up" in capsys.readouterr().err


def test_spawn_command_uses_the_current_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    def fake_popen(argv, **kwargs):
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    REAL_SPAWN_GUI()
    assert recorded["argv"] == [sys.executable, "-m", "scratchpad", "gui"]
    assert recorded["kwargs"]["start_new_session"] is True
    assert recorded["kwargs"]["stdin"] == cli.subprocess.DEVNULL
    assert recorded["kwargs"]["stdout"] == cli.subprocess.DEVNULL
    assert recorded["kwargs"]["stderr"] == cli.subprocess.DEVNULL


def test_spawned_gui_does_not_inherit_the_startup_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single use activation token must not live on in the long running GUI process."""
    recorded: dict[str, Any] = {}

    def fake_popen(argv, **kwargs):
        recorded["kwargs"] = kwargs
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("XDG_ACTIVATION_TOKEN", "wayland-token-42")
    monkeypatch.setenv("DESKTOP_STARTUP_ID", "startup-id-7")
    monkeypatch.setenv("SCRATCHPAD_DATA_DIR", "/somewhere/data")
    REAL_SPAWN_GUI()
    env = recorded["kwargs"]["env"]
    assert "XDG_ACTIVATION_TOKEN" not in env
    assert "DESKTOP_STARTUP_ID" not in env
    assert env["SCRATCHPAD_DATA_DIR"] == "/somewhere/data"  # the rest is inherited
    inherited = {k: v for k, v in os.environ.items()
                 if k not in ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID",
                              cli.ACTIVATION_TOKEN_ENV)}
    assert {k: v for k, v in env.items() if k != cli.ACTIVATION_TOKEN_ENV} == inherited
    assert os.environ["XDG_ACTIVATION_TOKEN"] == "wayland-token-42"  # our own is untouched


def test_spawned_gui_carries_the_activation_token_in_a_private_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``toggle`` never resends after an autostart, so the token rides in the env.

    Without it the window that the very first press of the global shortcut
    creates is presented with no startup id, which on Wayland means "demands
    attention" instead of focus.
    """
    recorded: dict[str, Any] = {}

    def fake_popen(argv, **kwargs):
        recorded["kwargs"] = kwargs
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("XDG_ACTIVATION_TOKEN", "wayland-token-42")
    REAL_SPAWN_GUI()
    assert recorded["kwargs"]["env"][cli.ACTIVATION_TOKEN_ENV] == "wayland-token-42"
    # ...and the GUI reads it back out of exactly that variable.
    assert cli.ACTIVATION_TOKEN_ENV == "SCRATCHPAD_ACTIVATION_TOKEN"


@pytest.mark.parametrize("token", ["", None])
def test_spawned_gui_gets_no_activation_variable_without_a_token(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    recorded: dict[str, Any] = {}

    def fake_popen(argv, **kwargs):
        recorded["kwargs"] = kwargs
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    if token is None:
        monkeypatch.delenv("XDG_ACTIVATION_TOKEN", raising=False)
    else:
        monkeypatch.setenv("XDG_ACTIVATION_TOKEN", token)
    # A stale one inherited from our own environment must not be passed on either.
    monkeypatch.setenv(cli.ACTIVATION_TOKEN_ENV, "stale-token")
    REAL_SPAWN_GUI()
    assert cli.ACTIVATION_TOKEN_ENV not in recorded["kwargs"]["env"]


# --------------------------------------------------------------------------- #
# status output
# --------------------------------------------------------------------------- #

def test_status_prints_the_fields_in_human_form(
    ipc_stub: SimpleNamespace, capsys: pytest.CaptureFixture
) -> None:
    ipc_stub.response = {"ok": True, "visible": False, "chars": 4211, "events": 908, "version": "0.1.0"}
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "running: yes" in out
    assert "window visible: no" in out
    assert "characters: 4211" in out
    assert "events: 908" in out
    assert "version: 0.1.0" in out


def test_status_prints_unexpected_fields_too(
    ipc_stub: SimpleNamespace, capsys: pytest.CaptureFixture
) -> None:
    ipc_stub.response = {"ok": True, "visible": True, "chars": 1, "events": 2, "attachments": 5}
    assert cli.main(["status"]) == 0
    assert "attachments: 5" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# scratchpad gui
# --------------------------------------------------------------------------- #

def test_gui_subcommand_calls_ui_app_main(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    module = types.ModuleType("scratchpad.ui.app")
    module.main = lambda: called.append(True) or None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scratchpad.ui.app", module)
    assert cli.main(["gui"]) == 0
    assert called == [True]


def test_gui_subcommand_reports_a_missing_toolkit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    broken = types.ModuleType("scratchpad.ui.app")

    def explode() -> None:  # pragma: no cover - never called
        raise AssertionError

    monkeypatch.setitem(sys.modules, "scratchpad.ui.app", broken)  # module without main()
    monkeypatch.delitem(sys.modules, "scratchpad.ui.app")

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "scratchpad.ui.app":
            raise ImportError("No module named 'gi'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert cli.main(["gui"]) == 1
    assert "cannot start the GUI" in capsys.readouterr().err


def test_cli_and_ipc_import_without_gi() -> None:
    """ARCHITECTURE.md section 1: only scratchpad.ui may depend on PyGObject."""
    code = (
        "import sys, scratchpad.cli, scratchpad.ipc;"
        "assert 'gi' not in sys.modules, sorted(m for m in sys.modules if m.startswith('gi'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# scratchpad-attach
# --------------------------------------------------------------------------- #

def test_attach_stdin_defaults_to_text_plain(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    ipc_stub.response = {"ok": True, "token": "7Fq9Lc2mwKa4Pz1TR8xNVe", "object_id": 3}
    set_stdin(monkeypatch, b"journal line 1\nline 2\n")
    assert cli.attach_main([]) == 0
    call = ipc_stub.calls[0]
    assert call.request == {"cmd": "attach", "kind": "text", "mime": "text/plain"}
    assert call.payload == b"journal line 1\nline 2\n"
    assert capsys.readouterr().out.strip() == "7Fq9Lc2mwKa4Pz1TR8xNVe"


def test_attach_file_guesses_mime_and_filename(
    ipc_stub: SimpleNamespace, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    ipc_stub.response = {"ok": True, "token": "t" * 22}
    path = tmp_path / "notes.txt"
    path.write_bytes(b"hello")
    assert cli.attach_main([str(path)]) == 0
    assert ipc_stub.calls[0].request == {
        "cmd": "attach",
        "kind": "text",
        "mime": "text/plain",
        "filename": "notes.txt",
    }
    assert ipc_stub.calls[0].payload == b"hello"


@pytest.mark.parametrize(
    ("filename", "mime", "kind"),
    [
        ("shot.png", "image/png", "image"),
        ("photo.jpeg", "image/jpeg", "image"),
        ("notes.txt", "text/plain", "text"),
        ("page.html", "text/html", "text"),
        ("archive.tar.gz", "application/x-tar", "file"),
        ("doc.pdf", "application/pdf", "file"),
    ],
)
def test_attach_kind_inference_from_the_file_name(
    ipc_stub: SimpleNamespace, tmp_path: Path, filename: str, mime: str, kind: str
) -> None:
    ipc_stub.response = {"ok": True, "token": "t" * 22}
    path = tmp_path / filename
    path.write_bytes(b"\x00\x01\x02")
    assert cli.attach_main([str(path)]) == 0
    assert ipc_stub.calls[0].request["mime"] == mime
    assert ipc_stub.calls[0].request["kind"] == kind


def test_attach_unknown_extension_falls_back_to_octet_stream(
    ipc_stub: SimpleNamespace, tmp_path: Path
) -> None:
    ipc_stub.response = {"ok": True, "token": "t" * 22}
    path = tmp_path / "blob.weird"
    path.write_bytes(b"xyz")
    assert cli.attach_main([str(path)]) == 0
    assert ipc_stub.calls[0].request["mime"] == "application/octet-stream"
    assert ipc_stub.calls[0].request["kind"] == "file"


def test_attach_explicit_mime_and_name_win(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ipc_stub.response = {"ok": True, "token": "t" * 22}
    set_stdin(monkeypatch, b"\x89PNG\r\n")
    assert cli.attach_main(["--mime", "image/png", "--name", "shot.png", "-"]) == 0
    assert ipc_stub.calls[0].request == {
        "cmd": "attach",
        "kind": "image",
        "mime": "image/png",
        "filename": "shot.png",
    }


def test_attach_stdin_with_name_guesses_from_the_name(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ipc_stub.response = {"ok": True, "token": "t" * 22}
    set_stdin(monkeypatch, b"GIF89a")
    assert cli.attach_main(["--name", "anim.gif"]) == 0
    assert ipc_stub.calls[0].request["mime"] == "image/gif"
    assert ipc_stub.calls[0].request["kind"] == "image"


def test_attach_uses_a_generous_timeout(ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    ipc_stub.response = {"ok": True, "token": "t" * 22}
    set_stdin(monkeypatch, b"data")
    assert cli.attach_main([]) == 0
    assert ipc_stub.calls[0].timeout == cli.PAYLOAD_TIMEOUT


def test_attach_rejects_empty_input(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    set_stdin(monkeypatch, b"")
    assert cli.attach_main([]) == 1
    assert "no input" in capsys.readouterr().err
    assert ipc_stub.calls == []


def test_attach_missing_file_is_an_error(ipc_stub: SimpleNamespace, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.attach_main([str(tmp_path / "gone.txt")])


def test_attach_without_a_token_in_the_response_fails(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    ipc_stub.response = {"ok": True}
    set_stdin(monkeypatch, b"data")
    assert cli.attach_main([]) == 1
    assert "did not return a token" in capsys.readouterr().err


def test_attach_sniffs_piped_image_data_without_a_name_or_mime(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A piped PNG is an image attachment, not text/plain."""
    ipc_stub.response = {"ok": True, "token": "t" * 22}
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 40
    set_stdin(monkeypatch, png)
    assert cli.attach_main([]) == 0
    assert ipc_stub.calls[0].request == {"cmd": "attach", "kind": "image", "mime": "image/png"}
    assert ipc_stub.calls[0].payload == png


def test_attach_sniffs_piped_binary_data_as_a_file(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ipc_stub.response = {"ok": True, "token": "t" * 22}
    blob = b"\x7fELF\x02\x01\x00\x00" + b"\x00" * 100 + b"payload"
    set_stdin(monkeypatch, blob)
    assert cli.attach_main([]) == 0
    assert ipc_stub.calls[0].request == {
        "cmd": "attach",
        "kind": "file",
        "mime": "application/octet-stream",
    }


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n\x00\x00", "image/png"),
        (b"GIF89a\x01\x00", "image/gif"),
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"plain text\nwith lines\n", "text/plain"),
        (b"", "text/plain"),
        (b"binary\x00data", "application/octet-stream"),
        (b"late nul" + b"x" * (8 << 10) + b"\x00", "text/plain"),  # only the head is read
    ],
)
def test_sniff_mime(data: bytes, expected: str) -> None:
    assert cli.sniff_mime(data) == expected


def test_attach_kind_for_mime_helper() -> None:
    assert cli.kind_for_mime(None) == "text"
    assert cli.kind_for_mime("") == "text"
    assert cli.kind_for_mime("text/x-log") == "text"
    assert cli.kind_for_mime("image/webp") == "image"
    assert cli.kind_for_mime("application/json") == "file"


# --------------------------------------------------------------------------- #
# scratchpad-insert
# --------------------------------------------------------------------------- #

def test_insert_joins_arguments_with_spaces(ipc_stub: SimpleNamespace) -> None:
    assert cli.insert_main(["temporary", "reminder"]) == 0
    assert ipc_stub.calls[0].request == {
        "cmd": "insert",
        "text": "temporary reminder",
        "where": "cursor",
    }


def test_insert_end_flag(ipc_stub: SimpleNamespace) -> None:
    assert cli.insert_main(["--end", "at the bottom"]) == 0
    assert ipc_stub.calls[0].request["where"] == "end"


def test_insert_reads_stdin(ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    set_stdin(monkeypatch, "grüße\n".encode())
    assert cli.insert_main([]) == 0
    assert ipc_stub.calls[0].request["text"] == "grüße\n"


def test_insert_replaces_undecodable_bytes(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_stdin(monkeypatch, b"ok\xff\n")
    assert cli.insert_main([]) == 0
    assert ipc_stub.calls[0].request["text"] == "ok�\n"


def test_insert_sends_a_large_text_as_the_payload(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Above the threshold the text travels as the payload: the JSON header is capped."""
    text = "x" * (2 * 1024 * 1024)
    set_stdin(monkeypatch, text.encode())
    assert cli.insert_main(["--end"]) == 0
    call = ipc_stub.calls[0]
    assert call.request == {"cmd": "insert", "where": "end"}
    assert "text" not in call.request
    assert call.payload == text.encode()
    assert call.timeout == cli.PAYLOAD_TIMEOUT


def test_insert_keeps_a_small_text_in_the_header(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "ä" * 1000
    set_stdin(monkeypatch, text.encode())
    assert cli.insert_main([]) == 0
    assert ipc_stub.calls[0].request == {"cmd": "insert", "text": text, "where": "cursor"}
    assert ipc_stub.calls[0].payload is None


def test_insert_around_the_payload_threshold(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    for size, in_header in ((cli.INSERT_PAYLOAD_THRESHOLD, True),
                            (cli.INSERT_PAYLOAD_THRESHOLD + 1, False)):
        ipc_stub.calls.clear()
        set_stdin(monkeypatch, b"y" * size)
        assert cli.insert_main([]) == 0
        assert ("text" in ipc_stub.calls[0].request) is in_header
        assert (ipc_stub.calls[0].payload is None) is in_header


def test_two_megabyte_insert_reaches_a_real_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a 2 MiB insert used to break on the 1 MiB header limit."""
    sock = tmp_path / "insert.sock"
    stub = StubServer(sock, default_handler)
    text = "läng" * 512 * 1024  # 2 Mi characters, 2.5 MiB of UTF-8
    try:
        set_stdin(monkeypatch, text.encode())
        assert cli.insert_main(["--socket", str(sock)]) == 0
    finally:
        stub.stop()
    request, payload = stub.requests[-1]
    assert request == {"cmd": "insert", "where": "cursor"}
    assert payload is not None and payload.decode("utf-8") == text


def test_insert_rejects_empty_input(
    ipc_stub: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    set_stdin(monkeypatch, b"")
    assert cli.insert_main([]) == 1
    assert "no input" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# against the real threaded stub server
# --------------------------------------------------------------------------- #

def test_end_to_end_against_a_running_stub_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    sock = tmp_path / "e2e.sock"
    stub = StubServer(sock, default_handler)
    try:
        monkeypatch.setenv("XDG_ACTIVATION_TOKEN", "tok-e2e")
        assert cli.main(["--socket", str(sock), "toggle"]) == 0
        assert cli.main(["--socket", str(sock), "status"]) == 0
        set_stdin(monkeypatch, b"piped log output\n")
        assert cli.attach_main(["--socket", str(sock)]) == 0
        assert cli.insert_main(["--socket", str(sock), "--end", "note"]) == 0
    finally:
        stub.stop()
    commands = [request["cmd"] for request, _ in stub.requests]
    assert commands == ["toggle", "status", "attach", "insert"]
    assert stub.requests[0][0]["activation_token"] == "tok-e2e"
    assert stub.requests[2][1] == b"piped log output\n"
    assert stub.requests[3][0] == {"cmd": "insert", "text": "note", "where": "end"}
    out = capsys.readouterr().out
    assert "0" * 22 in out
    assert "characters: 12" in out


# --------------------------------------------------------------------------- #
# install-shortcut
# --------------------------------------------------------------------------- #

@pytest.fixture
def cosmic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    config = home / ".config"
    config.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "COSMIC")
    return config / "cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom"


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("Super+grave", (["Super"], "grave")),
        ("super+grave", (["Super"], "grave")),
        ("Ctrl+Alt+space", (["Ctrl", "Alt"], "space")),
        ("alt+ctrl+space", (["Ctrl", "Alt"], "space")),
        ("Shift+Super+F1", (["Super", "Shift"], "F1")),
        ("Meta-grave", (["Super"], "grave")),
        ("F12", ([], "F12")),
    ],
)
def test_parse_key_spec(spec: str, expected: tuple[list[str], str]) -> None:
    assert cli.parse_key_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", "+", "Hyper+x", "Ctrl+shift"])
def test_parse_key_spec_rejects_nonsense(spec: str) -> None:
    with pytest.raises(ValueError):
        cli.parse_key_spec(spec)


def test_install_shortcut_creates_the_file(cosmic_home: Path) -> None:
    message = cli.install_cosmic_shortcut("Super+grave", "scratchpad toggle")
    assert cosmic_home.exists()
    assert cosmic_home.read_text() == (
        "{\n"
        '    (modifiers: [Super], key: "grave"): Spawn("scratchpad toggle"),\n'
        "}\n"
    )
    assert "created" in message


def backups(custom: Path) -> list[Path]:
    """Every backup the installer wrote, in its own directory outside the v1 key space."""
    backup_dir = custom.parents[2] / "scratchpad-shortcut-backups"  # <config>/cosmic/...
    assert backup_dir == cli.cosmic_backup_dir()
    return sorted(backup_dir.glob("custom.bak-*"))


def test_install_shortcut_keeps_existing_bindings_and_backs_up(cosmic_home: Path) -> None:
    cosmic_home.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        "{\n"
        '    (modifiers: [Super], key: "t"): Spawn("cosmic-term"),\n'
        '    (modifiers: [Super, Shift], key: "q"): Close,\n'
        "}\n"
    )
    cosmic_home.write_text(existing)

    message = cli.install_cosmic_shortcut("Super+grave", "scratchpad toggle")
    text = cosmic_home.read_text()

    assert '(modifiers: [Super], key: "t"): Spawn("cosmic-term"),' in text
    assert '(modifiers: [Super, Shift], key: "q"): Close,' in text
    assert '(modifiers: [Super], key: "grave"): Spawn("scratchpad toggle"),' in text
    assert text.count("Spawn(") == 2
    assert text.rstrip().endswith("}")

    written = backups(cosmic_home)
    assert len(written) == 1
    assert written[0].read_text() == existing
    assert "backup" in message
    # Nothing but the config key itself may remain in .../Shortcuts/v1/.
    assert [p.name for p in cosmic_home.parent.iterdir()] == ["custom"]


def test_install_shortcut_replaces_its_own_binding(cosmic_home: Path) -> None:
    cli.install_cosmic_shortcut("Super+grave", "scratchpad toggle")
    cli.install_cosmic_shortcut("Ctrl+Alt+space", "scratchpad toggle")
    text = cosmic_home.read_text()
    assert text.count("scratchpad toggle") == 1
    assert '(modifiers: [Ctrl, Alt], key: "space"): Spawn("scratchpad toggle"),' in text
    assert '"grave"' not in text


def test_install_shortcut_is_idempotent(cosmic_home: Path) -> None:
    cli.install_cosmic_shortcut("Super+grave", "scratchpad toggle")
    first = cosmic_home.read_text()
    message = cli.install_cosmic_shortcut("Super+grave", "scratchpad toggle")
    assert cosmic_home.read_text() == first
    assert "nothing to do" in message
    assert backups(cosmic_home) == []


def test_install_shortcut_removes_every_duplicate_of_its_own_binding(cosmic_home: Path) -> None:
    """Two stale entries for the same command must not survive as duplicate map keys."""
    cosmic_home.parent.mkdir(parents=True, exist_ok=True)
    cosmic_home.write_text(
        "{\n"
        '    (modifiers: [Super], key: "grave"): Spawn("scratchpad toggle"),\n'
        '    (modifiers: [Super], key: "t"): Spawn("cosmic-term"),\n'
        '    (modifiers: [Ctrl, Alt], key: "n"): Spawn("scratchpad toggle"),\n'
        "}\n"
    )
    message = cli.install_cosmic_shortcut("Shift+Super+F1", "scratchpad toggle")
    text = cosmic_home.read_text()
    assert text.count('Spawn("scratchpad toggle")') == 1
    assert '(modifiers: [Super, Shift], key: "F1"): Spawn("scratchpad toggle"),' in text
    assert '"grave"' not in text and '"n"' not in text
    assert 'Spawn("cosmic-term")' in text  # unrelated bindings stay
    assert "updated" in message

    # The result is stable: installing again changes nothing.
    assert "nothing to do" in cli.install_cosmic_shortcut("Shift+Super+F1", "scratchpad toggle")


def test_install_shortcut_refuses_a_key_used_by_another_command(cosmic_home: Path) -> None:
    cosmic_home.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        "{\n"
        '    (modifiers: [Super], key: "grave"): Spawn("other-app"),\n'
        "}\n"
    )
    cosmic_home.write_text(existing)
    with pytest.raises(cli.ShortcutConflict, match="already bound"):
        cli.install_cosmic_shortcut("Super+grave", "scratchpad toggle")
    assert cosmic_home.read_text() == existing  # untouched
    assert backups(cosmic_home) == []
    assert [p.name for p in cosmic_home.parent.iterdir()] == ["custom"]
    assert isinstance(cli.ShortcutConflict("x"), ValueError)


def test_install_shortcut_replaces_a_conflicting_binding_on_request(cosmic_home: Path) -> None:
    cosmic_home.parent.mkdir(parents=True, exist_ok=True)
    cosmic_home.write_text(
        "{\n"
        '    (modifiers: [Super], key: "grave"): Spawn("other-app"),\n'
        '    (modifiers: [Super], key: "t"): Spawn("cosmic-term"),\n'
        "}\n"
    )
    message = cli.install_cosmic_shortcut("Super+grave", "scratchpad toggle", replace=True)
    text = cosmic_home.read_text()
    assert '(modifiers: [Super], key: "grave"): Spawn("scratchpad toggle"),' in text
    assert "other-app" not in text
    assert 'Spawn("cosmic-term")' in text
    assert text.count('key: "grave"') == 1
    assert "replaced a conflicting binding" in message
    assert len(backups(cosmic_home)) == 1


def test_install_shortcut_conflict_ignores_modifier_order_and_spacing(cosmic_home: Path) -> None:
    cosmic_home.parent.mkdir(parents=True, exist_ok=True)
    cosmic_home.write_text(
        "{\n"
        '    (modifiers: [Alt,Ctrl], key: "space"): Spawn("other-app"),\n'
        "}\n"
    )
    with pytest.raises(cli.ShortcutConflict):
        cli.install_cosmic_shortcut("Ctrl+Alt+space", "scratchpad toggle")
    # A different combination is no conflict at all.
    cli.install_cosmic_shortcut("Super+grave", "scratchpad toggle")
    assert "other-app" in cosmic_home.read_text()


def test_install_shortcut_command_reports_a_conflict(
    cosmic_home: Path, capsys: pytest.CaptureFixture
) -> None:
    cosmic_home.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        "{\n"
        '    (modifiers: [Super], key: "grave"): Spawn("other-app"),\n'
        "}\n"
    )
    cosmic_home.write_text(existing)
    assert cli.main(["install-shortcut"]) == 1
    err = capsys.readouterr().err
    assert "already bound" in err
    assert "--replace" in err
    assert cosmic_home.read_text() == existing

    assert cli.main(["install-shortcut", "--replace"]) == 0
    assert 'Spawn("scratchpad toggle")' in cosmic_home.read_text()


def test_install_shortcut_refuses_a_file_without_a_closing_brace(cosmic_home: Path) -> None:
    cosmic_home.parent.mkdir(parents=True, exist_ok=True)
    cosmic_home.write_text("garbage without braces\n")
    with pytest.raises(ValueError, match="closing brace"):
        cli.install_cosmic_shortcut("Super+grave", "scratchpad toggle")
    assert cosmic_home.read_text() == "garbage without braces\n"


def test_install_shortcut_command_on_cosmic(cosmic_home: Path, capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["install-shortcut"]) == 0
    assert '"grave"' in cosmic_home.read_text()
    assert str(cosmic_home) in capsys.readouterr().out


def test_install_shortcut_command_with_custom_key(cosmic_home: Path) -> None:
    assert cli.main(["install-shortcut", "--key", "Ctrl+Alt+space"]) == 0
    assert '(modifiers: [Ctrl, Alt], key: "space")' in cosmic_home.read_text()


def test_install_shortcut_command_rejects_a_bad_key(
    cosmic_home: Path, capsys: pytest.CaptureFixture
) -> None:
    assert cli.main(["install-shortcut", "--key", "Hyper+x"]) == 1
    assert "unknown modifier" in capsys.readouterr().err
    assert not cosmic_home.exists()


@pytest.mark.parametrize(
    ("desktop", "needle"),
    [("KDE", "System Settings > Shortcuts > Custom"), ("GNOME", "Settings > Keyboard > Custom Shortcuts"),
     ("", "keyboard shortcut settings")],
)
def test_install_shortcut_prints_instructions_elsewhere(
    cosmic_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    desktop: str, needle: str
) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    assert cli.main(["install-shortcut"]) == 0
    out = capsys.readouterr().out
    assert needle in out
    assert "scratchpad toggle" in out
    assert not cosmic_home.exists()
