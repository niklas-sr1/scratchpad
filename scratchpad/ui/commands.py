"""The command registry: one definition per action, shared by menu and palette.

A :class:`Command` is the single description of something the user can do.
:class:`CommandRegistry` turns each one into

* a ``Gio.SimpleAction`` in the window's or the application's action map,
* an accelerator registered with ``Gtk.Application.set_accels_for_action``
  (which is also what makes the menu show the shortcut next to the label),
* a row in the command palette,
* an item in the primary menu, when a menu structure names it.

Nothing else in the UI may create actions: a command that is not in the registry
cannot be found in the palette, and undiscoverable commands are exactly what
spec sections 27 and 28 argue against.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, Gtk  # noqa: E402

__all__ = ["Command", "CommandRegistry", "MenuSection", "MenuSpec", "SEPARATOR"]

log = logging.getLogger(__name__)

#: Placeholder inside a menu section list that starts a new section.
SEPARATOR = None

#: ``(submenu label, [command id | SEPARATOR, ...])``
MenuSection = tuple[str, Sequence["str | None"]]
MenuSpec = Sequence[MenuSection]


@dataclass(frozen=True, slots=True)
class Command:
    """One user-visible action.

    ``shortcut`` is a GTK accelerator string such as ``"<Control><Shift>p"``.
    ``enabled`` is re-evaluated by :meth:`CommandRegistry.refresh`; a command
    without one is always enabled.  ``register_accel=False`` keeps the shortcut
    as documentation only, for keys that a widget handles itself (Escape, Tab).
    """

    id: str
    title: str
    category: str
    callback: Callable[[], None]
    shortcut: str | None = None
    enabled: Callable[[], bool] | None = None
    scope: str = "win"
    register_accel: bool = True
    description: str = ""

    @property
    def action_name(self) -> str:
        """Fully qualified action name, e.g. ``win.paste-as-attachment``."""
        return f"{self.scope}.{self.id}"

    @property
    def label(self) -> str:
        """``"Category: Title"``, the string the palette matches against."""
        return f"{self.category}: {self.title}"


class CommandRegistry:
    """Owns every command of one window."""

    def __init__(self, app: Gtk.Application, window: Gio.ActionMap) -> None:
        self.app = app
        self.window = window
        self._commands: dict[str, Command] = {}
        self._actions: dict[str, Gio.SimpleAction] = {}
        self._order: list[str] = []

    # -- registration --------------------------------------------------------

    def add(self, command: Command) -> Command:
        """Register ``command`` and create its action and accelerator."""
        if command.id in self._commands:
            raise ValueError(f"duplicate command id {command.id!r}")
        action = Gio.SimpleAction.new(command.id, None)
        action.connect("activate", lambda _a, _p, c=command: self._invoke(c))
        if command.enabled is not None:
            action.set_enabled(bool(_safe(command.enabled, True)))
        target = self.app if command.scope == "app" else self.window
        target.add_action(action)
        if command.shortcut and command.register_accel:
            self.app.set_accels_for_action(command.action_name, [command.shortcut])
        self._commands[command.id] = command
        self._actions[command.id] = action
        self._order.append(command.id)
        return command

    def add_many(self, commands: Iterable[Command]) -> None:
        """Register a batch of commands in order."""
        for command in commands:
            self.add(command)

    # -- lookup --------------------------------------------------------------

    def get(self, command_id: str) -> Command | None:
        """The command with this id, if any."""
        return self._commands.get(command_id)

    def all(self) -> list[Command]:
        """Every command, in registration order."""
        return [self._commands[cid] for cid in self._order]

    def action(self, command_id: str) -> Gio.SimpleAction | None:
        """The ``Gio.SimpleAction`` backing a command."""
        return self._actions.get(command_id)

    def is_enabled(self, command_id: str) -> bool:
        """Whether the command's action currently accepts activation."""
        action = self._actions.get(command_id)
        return bool(action is not None and action.get_enabled())

    # -- running -------------------------------------------------------------

    def activate(self, command_id: str) -> None:
        """Run a command by id (used by the palette and by tests)."""
        command = self._commands.get(command_id)
        if command is None:
            raise KeyError(command_id)
        if not self.is_enabled(command_id):
            log.debug("command %s is disabled; ignoring", command_id)
            return
        self._invoke(command)

    def _invoke(self, command: Command) -> None:
        try:
            command.callback()
        except Exception:  # noqa: BLE001 - a broken command must not kill the app
            log.exception("command %s failed", command.id)

    def refresh(self) -> None:
        """Re-evaluate every ``enabled`` predicate."""
        for command_id, command in self._commands.items():
            if command.enabled is None:
                continue
            action = self._actions[command_id]
            wanted = bool(_safe(command.enabled, True))
            if action.get_enabled() != wanted:
                action.set_enabled(wanted)

    # -- presentation --------------------------------------------------------

    def accel_label(self, command: Command) -> str:
        """Human readable shortcut, e.g. ``"Ctrl+Shift+P"``.  Empty when none."""
        if not command.shortcut:
            return ""
        ok, keyval, modifiers = Gtk.accelerator_parse(command.shortcut)
        if not ok:
            return command.shortcut
        return Gtk.accelerator_get_label(keyval, modifiers)

    def build_menu(self, structure: MenuSpec) -> Gio.Menu:
        """Build the primary menu model from ``(submenu, [ids])`` pairs.

        Unknown ids are skipped with a warning rather than raising: a menu is
        not worth a crash.
        """
        root = Gio.Menu()
        for title, ids in structure:
            submenu = Gio.Menu()
            section = Gio.Menu()
            for command_id in ids:
                if command_id is SEPARATOR:
                    if section.get_n_items():
                        submenu.append_section(None, section)
                        section = Gio.Menu()
                    continue
                command = self._commands.get(command_id)
                if command is None:
                    log.warning("menu references unknown command %r", command_id)
                    continue
                section.append(command.title, command.action_name)
            if section.get_n_items():
                submenu.append_section(None, section)
            if submenu.get_n_items():
                root.append_submenu(title, submenu)
        return root


def _safe(predicate: Callable[[], bool], default: bool) -> bool:
    try:
        return bool(predicate())
    except Exception:  # noqa: BLE001
        log.exception("command predicate failed")
        return default
