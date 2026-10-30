"""Explicit import and export of session settings."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from bandctl.settings_session import export_session, import_session


class SettingsScreen(ModalScreen):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, shaper, iface, detected_bps, down_budget, up_budget, rules, processes):
        super().__init__()
        self.shaper, self.iface, self.detected_bps = shaper, iface, detected_bps
        self.down_budget, self.up_budget = down_budget, up_budget
        self.rules, self.processes = list(rules), list(processes)

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Settings file", id="settings-title"),
            Static("Enter a file path, then choose Import or Export.", id="settings-help"),
            Input(placeholder="file path", id="settings-path"),
            Static("", id="settings-status"),
            Horizontal(Button("Import", id="settings-import"), Button("Export", id="settings-export"),
                       Button("Close", id="settings-close"), id="settings-buttons"),
            id="settings-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#settings-path", Input).focus()

    def _status(self, message: str, bad: bool = False) -> None:
        status = self.query_one("#settings-status", Static)
        status.set_class(bad, "-error")
        status.update(message)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "settings-close":
            self.action_close()
            return
        path = self.query_one("#settings-path", Input).value
        if not path.strip():
            self._status("Enter a file path", bad=True)
            return
        try:
            if action == "settings-export":
                export_session(path, self.shaper, self.iface, self.down_budget, self.up_budget, self.rules)
                self._status("Settings exported")
            elif action == "settings-import":
                settings = import_session(path, self.shaper, self.iface, self.detected_bps, self.processes)
                self.dismiss(settings)
        except ValueError as exc:
            self._status(str(exc), bad=True)

    def action_close(self) -> None:
        self.dismiss(None)
