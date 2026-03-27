"""ANSI escape code helpers and colour palette for ppcm."""

import sys


# ─── Primitives ──────────────────────────────────────────────────────────────

def cuu(n: int) -> str:
    """Cursor up n lines."""
    return f'\033[{n}A'


def el() -> str:
    """Erase to end of line."""
    return '\033[K'


def sgr(*codes) -> str:
    """Select Graphic Rendition (colour / style) escape sequence."""
    return f'\033[{";".join(str(c) for c in codes)}m'


def supports_color() -> bool:
    """Return True only when stdout is an interactive TTY."""
    return sys.stdout.isatty()


# ─── Palette (256-colour) ────────────────────────────────────────────────────

R        = sgr(0)                              # reset

# TUI list
SEL      = sgr(1, 38, 5, 255, 48, 5, 26)      # bold white on steel-blue (selected row)
ROW_EVEN = sgr(38, 5, 252)                     # light gray
ROW_ODD  = sgr(38, 5, 244)                     # medium gray
BORDER   = sgr(38, 5, 74)                      # sky-blue
STATUS   = sgr(38, 5, 245)                     # medium gray
COLHDR   = sgr(1, 38, 5, 255, 48, 5, 237)     # bold white on very-dark

# Help formatter
H_LABEL  = sgr(1, 38, 5, 74)                  # bold sky-blue  (usage:, section headers)
H_PROG   = sgr(1, 38, 5, 255)                 # bold white     (prog name)
H_BRACK  = sgr(38, 5, 180)                    # tan            ([options])
H_OPT    = sgr(38, 5, 222)                    # gold           (--flag)
H_META   = sgr(38, 5, 116)                    # teal           (METAVAR)
H_HELP   = sgr(38, 5, 245)                    # dim gray       (help text)
H_CMD    = sgr(38, 5, 114)                    # green          (ppcm in examples)
