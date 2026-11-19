"""Practice colors. The public page and the desk use the same profile."""

from __future__ import annotations

PROFILES = ("desk", "ink", "forest", "sea", "custom")
MODES = ("light", "dark")

# ink, paper, line, accent, card, note.
# Desk is a manuscript sheet: warm paper, a pressed coral for actions, hairlines.
# Light desk paper stays #f6f1e7. Sea dark paper stays #101820.
_PALETTES = {
    ("desk", "light"): ("#141413", "#f6f1e7", "#e6dfd8", "#a9583e", "#fbf7f0", "#f3eadb"),
    ("desk", "dark"): ("#faf9f5", "#181715", "#3a342c", "#e7a18c", "#252320", "#2e2923"),
    ("ink", "light"): ("#141413", "#f7f6f3", "#e3e1db", "#141413", "#fffcf8", "#efeee8"),
    ("ink", "dark"): ("#f7f6f3", "#121211", "#2c2c2a", "#f7f6f3", "#1c1c1a", "#242422"),
    ("forest", "light"): ("#14241b", "#f3f6f1", "#d3ddd4", "#1f6b45", "#fbfcf8", "#e6f0e8"),
    ("forest", "dark"): ("#e7f2ea", "#101a14", "#24382c", "#8fbf9a", "#17241c", "#1c2e24"),
    ("sea", "light"): ("#142028", "#f3f6f8", "#d0dde4", "#1d5c86", "#f8fbfc", "#e6eef3"),
    ("sea", "dark"): ("#e7f1f6", "#101820", "#243440", "#7eb6d9", "#162028", "#1c2a34"),
}


def practice_defaults() -> dict:
    return {"profile": "desk", "mode": "light", "accent": "", "paper": "", "ink": ""}


def resolve_theme(appearance: dict | None) -> dict:
    """CSS colors for a stored appearance. Invalid custom colors fall back to Desk."""
    stored = practice_defaults()
    if appearance:
        stored.update({key: appearance.get(key) or stored[key] for key in stored})
    profile = stored["profile"] if stored["profile"] in PROFILES else "desk"
    mode = stored["mode"] if stored["mode"] in MODES else "light"
    if profile == "custom" and _hex(stored["accent"]) and _hex(stored["paper"]) and _hex(stored["ink"]):
        ink = _full(stored["ink"])
        paper = _full(stored["paper"])
        accent = _full(stored["accent"])
        colors = (
            ink,
            paper,
            _mix(paper, ink, 0.22),
            accent,
            _mix(paper, ink, 0.06),
            _mix(paper, accent, 0.14),
        )
    else:
        if profile == "custom":
            profile = "desk"
        colors = _PALETTES[(profile, mode)]
    ink, paper, line, accent, card, note = colors
    return {
        "scheme": mode,
        "ink": ink,
        "paper": paper,
        "line": line,
        "accent": accent,
        "card": card,
        "note": note,
        "on_accent": "#1c1915" if _luminance(accent) > 0.62 else "#ffffff",
        "error": "#c64545" if mode == "light" else "#f0a8a8",
    }


def css_root(theme: dict) -> str:
    return (
        ":root { color-scheme: "
        + theme["scheme"]
        + "; --ink: "
        + theme["ink"]
        + "; --paper: "
        + theme["paper"]
        + "; --line: "
        + theme["line"]
        + "; --accent: "
        + theme["accent"]
        + "; --card: "
        + theme["card"]
        + "; --note: "
        + theme["note"]
        + "; --on-accent: "
        + theme["on_accent"]
        + "; --error: "
        + theme["error"]
        + "; }"
    )


def _hex(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("#"):
        return False
    body = value[1:]
    if len(body) not in (3, 6):
        return False
    return all(ch in "0123456789abcdefABCDEF" for ch in body)


def _full(value: str) -> str:
    body = value[1:]
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    return "#" + body.lower()


def _channel(value: str) -> tuple[int, int, int]:
    body = _full(value)[1:]
    return tuple(int(body[index : index + 2], 16) for index in (0, 2, 4))


def _mix(start: str, end: str, amount: float) -> str:
    left = _channel(start)
    right = _channel(end)

    def blend(index: int) -> int:
        return round(left[index] * (1 - amount) + right[index] * amount)

    return f"#{blend(0):02x}{blend(1):02x}{blend(2):02x}"


def _luminance(value: str) -> float:
    red, green, blue = _channel(value)
    return (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255
