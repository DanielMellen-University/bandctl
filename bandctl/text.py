"""Helpers for displaying external text on a terminal."""


def terminal_text(text: str) -> str:
    """Replace controls and unpaired surrogates with visible placeholders."""
    return "".join(
        "?" if ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF else char
        for char in text
    )
