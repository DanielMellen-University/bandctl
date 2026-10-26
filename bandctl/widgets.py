"""Screens that are not the main process view."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class RootRequired(ModalScreen[None]):
    """Shown once at startup when the process is not root."""

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("bandctl needs root to set limits.", id="root-title"),
            Static(
                "This session can list processes and nothing else. "
                "Limits are not applied, and no tc rules are installed.",
                id="root-body",
            ),
            Static(self.command, id="root-command"),
            Button("View processes", id="root-ok"),
            id="dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "root-ok":
            self.dismiss(None)
