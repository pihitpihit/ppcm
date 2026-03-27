"""PCM file browser – bottom-pane list TUI."""

import os
import sys
import shutil
import termios
import tty
import select

from .ansi import (
    cuu, el, sgr,
    R, SEL, ROW_EVEN, ROW_ODD, BORDER, STATUS, COLHDR,
)
from .pcm_utils import pcm_duration, fmt_size

MAX_VISIBLE = 15
# top-border + status + col-header + MAX_VISIBLE rows + bottom-border
TUI_H = 3 + MAX_VISIBLE + 1


# ─── Korean IME transparency (두벌식 standard layout) ────────────────────────
#
# When macOS Korean IME is active the Cocoa input layer sits above tty.setraw(),
# so the terminal receives Hangul Compatibility Jamo (U+3130–U+318F) instead of
# the originating ASCII key.  Map each jamo back to its physical key so that
# navigation shortcuts work regardless of the current input mode.
#
# Layout reference (두벌식):
#   q=ㅂ w=ㅈ e=ㄷ r=ㄱ t=ㅅ  y=ㅛ u=ㅕ i=ㅑ o=ㅐ p=ㅔ
#   a=ㅁ s=ㄴ d=ㅇ f=ㄹ g=ㅎ  h=ㅗ j=ㅓ k=ㅏ l=ㅣ
#   z=ㅋ x=ㅌ c=ㅊ v=ㅍ         b=ㅠ n=ㅜ m=ㅡ

_KO_TO_ASCII: dict = {
    # consonants
    'ㅂ': 'q', 'ㅈ': 'w', 'ㄷ': 'e', 'ㄱ': 'r', 'ㅅ': 't',
    'ㅁ': 'a', 'ㄴ': 's', 'ㅇ': 'd', 'ㄹ': 'f', 'ㅎ': 'g',
    'ㅋ': 'z', 'ㅌ': 'x', 'ㅊ': 'c', 'ㅍ': 'v',
    # vowels
    'ㅛ': 'y', 'ㅕ': 'u', 'ㅑ': 'i', 'ㅐ': 'o', 'ㅔ': 'p',
    'ㅗ': 'h', 'ㅓ': 'j', 'ㅏ': 'k', 'ㅣ': 'l',
    'ㅠ': 'b', 'ㅜ': 'n', 'ㅡ': 'm',
}


# ─── Key reader ──────────────────────────────────────────────────────────────

def read_key() -> str:
    ch = sys.stdin.read(1)
    # Transparently remap Korean jamo to the originating ASCII key
    ch = _KO_TO_ASCII.get(ch, ch)
    if ch == '\x1b':
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if r:
            ch2 = sys.stdin.read(1)
            if ch2 == '[':
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if r:
                    ch3 = sys.stdin.read(1)
                    return {'A': 'UP', 'B': 'DOWN',
                            'C': 'RIGHT', 'D': 'LEFT'}.get(ch3, f'ESC[{ch3}')
        return 'ESC'
    if ch in ('\r', '\n'): return 'ENTER'
    if ch == '\x03':       return 'CTRL_C'
    return ch


# ─── List TUI ────────────────────────────────────────────────────────────────

class ListTUI:
    """Wizard-style bottom-pane file browser for PCM files.

    Occupies TUI_H lines at the bottom of the current terminal output.
    Previous terminal content is preserved above.

    Returns the selected file path from run(), or None if the user quit.
    """

    def __init__(self, files: list, base_dir: str, use_color: bool):
        self.files     = files
        self.base_dir  = base_dir
        self.use_color = use_color
        self.cursor    = 0
        self.offset    = 0
        self.selected  = None

    # ── rendering ────────────────────────────────────────────────────────────

    def _c(self, text: str, *codes) -> str:
        if not self.use_color:
            return text
        return ''.join(codes) + text + R

    def _render(self) -> list:
        w     = shutil.get_terminal_size().columns
        total = len(self.files)
        lines = []

        # top border
        title = "  PCM Browser  "
        side  = (w - len(title) - 2) // 2
        extra = w - len(title) - 2 - side * 2
        top   = ("─" * side + title + "─" * (side + extra))[:w]
        lines.append(self._c(top.ljust(w), BORDER))

        # status / key hints
        pos   = f"  {self.cursor + 1}/{total}"
        hints = "[j/k/↑↓] nav  [↵] select  [q/ESC] quit  "
        pad   = max(w - len(pos) - len(hints), 1)
        lines.append(self._c((pos + " " * pad + hints)[:w].ljust(w), STATUS))

        # column header
        num_w  = len(str(total))
        path_w = max(w - (num_w + 4) - 20, 10)
        col_hdr = f"  {'#':>{num_w}}  {'FILE':<{path_w}}{'SIZE':>8}{'DURATION':>12}"
        lines.append(self._c(col_hdr[:w].ljust(w), COLHDR))

        # file entries
        visible = self.files[self.offset: self.offset + MAX_VISIBLE]
        for i, fp in enumerate(visible):
            idx    = self.offset + i
            is_cur = (idx == self.cursor)

            try:
                sz      = os.path.getsize(fp)
                dur     = pcm_duration(fp)
                sz_str  = fmt_size(sz)
                dur_str = f"{dur:.3f}s"
            except OSError:
                sz_str = dur_str = "?"

            rel = os.path.relpath(fp, self.base_dir)
            if len(rel) > path_w:
                rel = "…" + rel[-(path_w - 1):]

            row = f"  {idx + 1:>{num_w}}  {rel:<{path_w}}{sz_str:>8}{dur_str:>12}"
            row = row[:w].ljust(w)

            if is_cur:
                lines.append(self._c(row, SEL))
            elif i % 2 == 0:
                lines.append(self._c(row, ROW_EVEN))
            else:
                lines.append(self._c(row, ROW_ODD))

        # pad empty slots
        for _ in range(MAX_VISIBLE - len(visible)):
            lines.append(" " * w)

        # bottom border
        lines.append(self._c(("─" * w)[:w], BORDER))

        return lines

    def _draw(self, lines: list):
        buf = []
        for ln in lines:
            buf.append('\r' + ln + el() + '\n')
        sys.stdout.write(''.join(buf))
        sys.stdout.flush()

    # ── event loop ───────────────────────────────────────────────────────────

    def run(self):
        # reserve space at the bottom without clearing existing output
        sys.stdout.write('\n' * TUI_H + cuu(TUI_H))
        sys.stdout.flush()
        self._draw(self._render())

        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                key = read_key()

                if key in ('q', 'Q', 'ESC', 'CTRL_C'):
                    break
                elif key in ('k', 'UP'):
                    self.cursor = (self.cursor - 1) % len(self.files)
                    if self.cursor < self.offset:
                        self.offset = self.cursor
                    elif self.cursor == len(self.files) - 1:
                        self.offset = max(0, len(self.files) - MAX_VISIBLE)
                elif key in ('j', 'DOWN'):
                    self.cursor = (self.cursor + 1) % len(self.files)
                    if self.cursor == 0:
                        self.offset = 0
                    elif self.cursor >= self.offset + MAX_VISIBLE:
                        self.offset = self.cursor - MAX_VISIBLE + 1
                elif key == 'ENTER':
                    self.selected = self.files[self.cursor]
                    break

                sys.stdout.write(cuu(TUI_H))
                self._draw(self._render())

        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

        # clear TUI area, leave cursor at the top of where TUI was
        sys.stdout.write(cuu(TUI_H))
        for _ in range(TUI_H):
            sys.stdout.write('\r' + el() + '\n')
        sys.stdout.write(cuu(TUI_H))
        sys.stdout.flush()

        return self.selected
