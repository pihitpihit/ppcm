"""PCM file browser – bottom-pane list TUI."""

import os
import sys
import shutil
import termios
import tty
import select

from ansi import (
    cuu, el, sgr,
    R, SEL, ROW_EVEN, ROW_ODD, BORDER, STATUS, COLHDR,
)
from pcm_utils import pcm_duration, fmt_size

MAX_VISIBLE = 15
# top-border + status + col-header + MAX_VISIBLE rows + bottom-border
TUI_H = 3 + MAX_VISIBLE + 1


# ─── Key reader ──────────────────────────────────────────────────────────────

def read_key() -> str:
    ch = sys.stdin.read(1)
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
        path_w  = max(w - 22, 10)
        col_hdr = f"  {'FILE':<{path_w}}{'SIZE':>8}{'DURATION':>12}"
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

            row = f"  {rel:<{path_w}}{sz_str:>8}{dur_str:>12}"
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
                    if self.cursor > 0:
                        self.cursor -= 1
                        if self.cursor < self.offset:
                            self.offset = self.cursor
                elif key in ('j', 'DOWN'):
                    if self.cursor < len(self.files) - 1:
                        self.cursor += 1
                        if self.cursor >= self.offset + MAX_VISIBLE:
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
