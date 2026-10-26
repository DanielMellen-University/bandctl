"""Screens that are not the main process view."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from bandctl.rates import RateError, parse_percent
from bandctl.rules import AutoRule


class RootRequired(ModalScreen[None]):
    """Shown once at startup when the process is not root."""

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("bandctl needs root to set limits.", id="root-title"),
            Static(
                "You can view processes and traffic. "
                "Limits are off, and no tc rules are installed.",
                id="root-body",
            ),
            Static(self.command, id="root-command"),
            Button("View processes", id="root-ok"),
            id="dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "root-ok":
            self.dismiss(None)


class RulesScreen(ModalScreen[list[AutoRule]]):
    """Edit session rules, in order of precedence."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, rules: list[AutoRule]) -> None:
        super().__init__()
        self.rules = list(rules)

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Auto-apply rules", id="rules-title"),
            Static("Name match. First wins. Rules end on quit.", id="rules-help"),
            DataTable(id="rule-table", cursor_type="row"),
            Horizontal(
                Input(placeholder="name contains", id="rule-name"),
                Input(placeholder="down %", id="rule-down"),
                Input(placeholder="up %", id="rule-up"),
                id="rule-fields",
            ),
            Static("", id="rule-status"),
            Horizontal(
                Button("Add", id="rule-add"),
                Button("Delete", id="rule-delete"),
                Button("Close", id="rule-close"),
                id="rule-buttons",
            ),
            id="rules-dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one("#rule-table", DataTable)
        table.add_column("Name contains", width=22)
        table.add_column("Down %", width=7)
        table.add_column("Up %", width=7)
        self._render_rules()
        self.query_one("#rule-name", Input).focus()

    def _render_rules(self) -> None:
        table = self.query_one("#rule-table", DataTable)
        table.clear()
        for rule in self.rules:
            table.add_row(rule.name, str(rule.down_pct or "-"), str(rule.up_pct or "-"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rule-add":
            try:
                down = parse_percent(self.query_one("#rule-down", Input).value, allow_empty=True)
                up = parse_percent(self.query_one("#rule-up", Input).value, allow_empty=True)
                rule = AutoRule(self.query_one("#rule-name", Input).value, down, up)
            except (RateError, ValueError) as exc:
                self.query_one("#rule-status", Static).update(str(exc))
                return
            self.rules.append(rule)
            self._render_rules()
            for query in ("#rule-name", "#rule-down", "#rule-up"):
                self.query_one(query, Input).value = ""
            self.query_one("#rule-status", Static).update("Rule added")
            self.query_one("#rule-name", Input).focus()
        elif event.button.id == "rule-delete":
            table = self.query_one("#rule-table", DataTable)
            if self.rules:
                self.rules.pop(table.cursor_row)
                self._render_rules()
                self.query_one("#rule-status", Static).update("Rule deleted")
        elif event.button.id == "rule-close":
            self.action_close()

    def action_close(self) -> None:
        self.dismiss(self.rules)
