"""Helpers for displaying external text on a terminal."""


def terminal_text(text: str) -> str:
    """Replace ASCII and C1 control characters with visible placeholders."""
    return "".join("?" if ord(char) < 32 or 127 <= ord(char) <= 159 else char for char in text)
