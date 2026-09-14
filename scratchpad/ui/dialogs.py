"""Dialog helpers: confirmations, the large-paste guardrail, redaction, reports.

Everything here is built on :class:`Adw.AlertDialog` and is asynchronous: the
helpers take callbacks and return immediately, because GTK4 has no nested main
loop for dialogs any more.

Wording rules (spec section 26) that this module is the single home for:

* the destructive operation is called **"Remove permanently from scratchpad
  history"**,
* every destructive dialog says that it rewrites history and that copies may
  survive in backups and filesystem snapshots,
* the phrase "secure erase" is never used, and no forensic guarantee is implied.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

__all__ = [
    "REDACT_HEADING",
    "REDACT_CAVEAT",
    "REDACT_OCCURRENCE",
    "REDACT_EVERYWHERE",
    "REDACT_PURGE",
    "confirm",
    "error",
    "info",
    "large_paste",
    "redaction_choice",
    "report",
    "ask_text",
    "format_report",
]

log = logging.getLogger(__name__)

#: The one sanctioned name of the destructive operation (spec section 26).
REDACT_HEADING = "Remove permanently from scratchpad history"

#: Appended to every destructive dialog body.
REDACT_CAVEAT = (
    "This rewrites the stored scratchpad history: the affected content is removed "
    "from every retained historical state and the event log is replaced.\n\n"
    "This is not a forensic deletion. Copies may survive in backups, filesystem "
    "snapshots, swap or unreferenced disk blocks outside this application's control."
)

REDACT_OCCURRENCE = "occurrence"
REDACT_EVERYWHERE = "everywhere"
REDACT_PURGE = "purge"


def _present(dialog: Adw.AlertDialog, parent: Gtk.Widget | None) -> None:
    dialog.present(parent)


def confirm(
    parent: Gtk.Widget | None,
    heading: str,
    body: str,
    *,
    confirm_label: str = "Continue",
    destructive: bool = True,
    on_confirm: Callable[[], None],
    cancel_label: str = "Cancel",
) -> Adw.AlertDialog:
    """Ask a yes/no question; run ``on_confirm`` only on the affirmative answer."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", cancel_label)
    dialog.add_response("confirm", confirm_label)
    dialog.set_response_appearance(
        "confirm",
        Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED,
    )
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        if response == "confirm":
            on_confirm()

    dialog.connect("response", on_response)
    _present(dialog, parent)
    return dialog


def error(parent: Gtk.Widget | None, heading: str, body: str) -> Adw.AlertDialog:
    """Report a failure.  Nothing in this application may crash instead."""
    log.warning("%s: %s", heading, body)
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("ok", "Close")
    dialog.set_default_response("ok")
    dialog.set_close_response("ok")
    _present(dialog, parent)
    return dialog


def info(parent: Gtk.Widget | None, heading: str, body: str) -> Adw.AlertDialog:
    """Show a result the user asked for (no choice to make)."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("ok", "Close")
    dialog.set_default_response("ok")
    dialog.set_close_response("ok")
    _present(dialog, parent)
    return dialog


def large_paste(
    parent: Gtk.Widget | None,
    *,
    lines: int,
    chars: int,
    on_inline: Callable[[], None],
    on_attachment: Callable[[], None],
) -> Adw.AlertDialog:
    """The large-paste guardrail (spec section 30).

    Exactly two affirmative choices, plus Cancel.  Counts are shown with
    thousands separators, because "18,431 lines" is the whole point of the
    dialog.
    """
    dialog = Adw.AlertDialog(
        heading="This is a very large inline paste.",
        body=f"The clipboard holds {lines:,} lines ({chars:,} characters).\n\n"
             "Storing it as an attachment keeps the scratchpad readable: the text is "
             "kept outside the document and only its token is inserted.",
    )
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("inline", "Insert inline")
    dialog.add_response("attachment", "Store as attachment")
    dialog.set_response_appearance("attachment", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("attachment")
    dialog.set_close_response("cancel")

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        if response == "inline":
            on_inline()
        elif response == "attachment":
            on_attachment()

    dialog.connect("response", on_response)
    _present(dialog, parent)
    return dialog


def redaction_choice(
    parent: Gtk.Widget | None,
    *,
    snippet: str,
    can_purge: bool,
    can_redact_occurrence: bool = True,
    on_choice: Callable[[str], None],
) -> Adw.AlertDialog:
    """Offer the three redaction modes of spec section 24.

    ``on_choice`` receives :data:`REDACT_OCCURRENCE`, :data:`REDACT_EVERYWHERE`
    or :data:`REDACT_PURGE`.  The purge option only exists when the selection is
    a valid attachment token (``can_purge``); the single-occurrence option only
    when a historical state is selected to bound it (``can_redact_occurrence``),
    because without one it would silently mean "everywhere".
    """
    preview = snippet if len(snippet) <= 120 else snippet[:117] + "..."
    body = f"Selected content:\n{preview!r}\n\n{REDACT_CAVEAT}"
    if not can_redact_occurrence:
        body += (
            "\n\nRemoving only one historical occurrence needs a selection inside a "
            "reconstructed state; open the history panel, pick a time and select the "
            "text there."
        )
    dialog = Adw.AlertDialog(heading=REDACT_HEADING, body=body)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_margin_top(6)
    options: list[tuple[str, str]] = []
    if can_redact_occurrence:
        options.append((REDACT_OCCURRENCE, "Redact this historical occurrence"))
    options.append((REDACT_EVERYWHERE, "Redact this exact content everywhere"))
    if can_purge:
        options.append((REDACT_PURGE, "Purge this attachment from history"))
    buttons: dict[str, Gtk.CheckButton] = {}
    group: Gtk.CheckButton | None = None
    for key, label in options:
        button = Gtk.CheckButton(label=label)
        if group is None:
            group = button
            button.set_active(True)
        else:
            button.set_group(group)
        box.append(button)
        buttons[key] = button
    dialog.set_extra_child(box)

    dialog.add_response("cancel", "Cancel")
    dialog.add_response("remove", "Remove permanently")
    dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        if response != "remove":
            return
        for key, button in buttons.items():
            if button.get_active():
                on_choice(key)
                return

    dialog.connect("response", on_response)
    _present(dialog, parent)
    return dialog


_REPORT_LABELS = {
    "events_before": "Events before",
    "events_after": "Events after",
    "occurrences_removed": "Occurrences removed",
    "states_changed": "Historical states changed",
    "attachments_deleted": "Attachments deleted",
    "blobs_deleted": "Blobs deleted",
    "bytes_freed": "Bytes freed",
    "attachments_scanned": "Attachments scanned",
    "scanned_events": "Events replayed",
    "referenced_ids": "Still referenced",
    "deleted_ids": "Attachments deleted",
    "referenced": "Still referenced",
    "unreferenced": "Unreferenced",
    "dry_run": "Preview only",
    "window": "Time window",
}

#: Fields holding wall clock nanoseconds, rendered as local times instead.
_TIME_FIELDS = frozenset({"window", "time_range"})


def _local_time(wall_ns: int) -> str:
    """A wall clock nanosecond value as a readable local timestamp."""
    try:
        return datetime.fromtimestamp(wall_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):  # pragma: no cover - absurd clocks
        return f"{wall_ns} ns"


def _render_value(name: str, value: Any) -> str:
    """One report field as the user should read it, not as it is stored."""
    if value is None:
        return "whole history" if name in _TIME_FIELDS else "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if name in _TIME_FIELDS and isinstance(value, (tuple, list)) and len(value) == 2:
        try:
            return f"{_local_time(int(value[0]))} .. {_local_time(int(value[1]))}"
        except (TypeError, ValueError):  # pragma: no cover - not a time after all
            pass
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (list, tuple, set)):
        return f"{len(value):,}"
    return str(value)


def format_report(obj: Any) -> str:
    """Render a ``RedactionReport``/``GcReport`` for a dialog.

    The report's own ``summary()`` sentence comes first when it has one, because
    it is the only part written for a human; the field table below it is the
    detail, with wall clock windows as local times and flags as yes/no rather
    than as raw nanoseconds and ``True``.

    Deliberately generic: the redaction module is written by another agent and
    may carry more fields than the contract names.
    """
    if obj is None:
        return "(no report)"
    lines: list[str] = []
    summary = getattr(obj, "summary", None)
    if callable(summary):
        try:
            sentence = str(summary()).strip()
        except Exception:  # noqa: BLE001 - a report must still be shown
            log.exception("report summary failed")
            sentence = ""
        if sentence:
            lines.append(sentence)
            lines.append("")

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        values = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    elif isinstance(obj, dict):
        values = dict(obj)
    else:
        values = {
            name: getattr(obj, name)
            for name in dir(obj)
            if not name.startswith("_") and not callable(getattr(obj, name, None))
        }
    for name, value in values.items():
        label = _REPORT_LABELS.get(name, name.replace("_", " ").capitalize())
        lines.append(f"{label}: {_render_value(name, value)}")
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) if lines else "(empty report)"


def report(
    parent: Gtk.Widget | None,
    heading: str,
    body: str,
    payload: Any,
    *,
    confirm_label: str | None = None,
    on_confirm: Callable[[], None] | None = None,
) -> Adw.AlertDialog:
    """Show a report; optionally as the confirmation step of a destructive run."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    view = Gtk.Label(label=format_report(payload), xalign=0.0)
    view.add_css_class("monospace")
    view.set_selectable(True)
    view.set_wrap(True)
    view.set_margin_top(6)
    dialog.set_extra_child(view)
    if confirm_label is not None and on_confirm is not None:
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("confirm", confirm_label)
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "confirm":
                on_confirm()

        dialog.connect("response", on_response)
    else:
        dialog.add_response("ok", "Close")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
    _present(dialog, parent)
    return dialog


def ask_text(
    parent: Gtk.Widget | None,
    heading: str,
    body: str,
    *,
    initial: str = "",
    placeholder: str = "",
    confirm_label: str = "Apply",
    on_accept: Callable[[str], None],
) -> Adw.AlertDialog:
    """Ask for one line of text (used for the global shortcut key spec)."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    entry = Gtk.Entry(text=initial)
    entry.set_placeholder_text(placeholder)
    entry.set_activates_default(True)
    entry.set_margin_top(6)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("accept", confirm_label)
    dialog.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("accept")
    dialog.set_close_response("cancel")

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        if response == "accept":
            on_accept(entry.get_text().strip())

    dialog.connect("response", on_response)
    _present(dialog, parent)
    return dialog
