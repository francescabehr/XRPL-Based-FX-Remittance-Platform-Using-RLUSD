"""Palette 3 ("coastal") theme: every text/background pair the UI uses passes WCAG AA.

The tokens are read straight from design-system.css, so a future colour change that
breaks contrast fails here rather than on screen.
"""
import re
from pathlib import Path

CSS = Path("frontend/static/css/design-system.css").read_text()
ROOT = CSS[CSS.index(":root {"):CSS.index("/* 2. Bootstrap mapping")]
TOKENS = dict(re.findall(r"(--[\w-]+):\s*(#[0-9A-Fa-f]{6})\s*;", ROOT))


def luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def colour(name: str) -> str:
    return "#FFFFFF" if name == "white" else TOKENS[name]


# (foreground, background) pairs where the foreground is TEXT: need ≥ 4.5:1.
TEXT_PAIRS = [
    ("white", "--accent"), ("white", "--accent-hover"), ("white", "--accent-active"),
    ("--accent", "--bg"), ("--accent", "--surface"), ("--accent", "--accent-soft"),
    ("--accent", "--chalk"), ("--accent", "--chalk-soft"),
    ("--highlight-ink", "--highlight"), ("--highlight-ink", "--highlight-hover"),
    ("--highlight-ink", "--highlight-soft"),
    ("--sage-ink", "--sage"), ("--sage-ink", "--sage-soft"), ("--sage-ink", "--sage-active"),
    ("--text", "--bg"), ("--text", "--surface"), ("--text", "--chalk-soft"),
    ("--text-muted", "--bg"), ("--text-muted", "--surface"), ("--text-muted", "--chalk-soft"),
]
# Graphics beside or on a surface (bars, rings, routes, control borders): need ≥ 3:1.
GRAPHIC_PAIRS = [
    ("--highlight", "--surface"), ("--accent-mid", "--surface"), ("--border-input", "--surface"),
    ("--sage", "--accent"), ("--chalk", "--accent"),
]


def test_palette_3_tokens_are_in_place():
    assert TOKENS["--accent"].upper() == "#933B5B"        # Amaranth
    assert TOKENS["--highlight"].upper() == "#B5728A"     # Thulian Pink
    assert TOKENS["--sage"].upper() == "#AABAAE"          # Brook Green
    assert TOKENS["--chalk"].upper() == "#E3D6BF"         # Chalk
    assert TOKENS["--olive"].upper() == "#9F9679"         # Pomelo Olive


def test_text_pairs_pass_aa():
    failures = [(fg, bg, round(contrast(colour(fg), colour(bg)), 2))
                for fg, bg in TEXT_PAIRS if contrast(colour(fg), colour(bg)) < 4.5]
    assert not failures, failures


def test_graphic_pairs_pass_3_to_1():
    failures = [(fg, bg, round(contrast(colour(fg), colour(bg)), 2))
                for fg, bg in GRAPHIC_PAIRS if contrast(colour(fg), colour(bg)) < 3]
    assert not failures, failures


def test_old_palette_is_gone():
    old = ["#FF69B4", "#1B4B4F", "#069494", "#D9C8F5", "#EDE4FB", "#4A2E86", "#102E31",
           "255, 105, 180", "27, 75, 79", "--lavender"]
    for path in ("frontend/static/css/design-system.css", "frontend/static/js/globe.js",
                 "frontend/templates/dev/styleguide.html"):
        text = Path(path).read_text()
        left = [value for value in old if value.lower() in text.lower()]
        assert not left, (path, left)
