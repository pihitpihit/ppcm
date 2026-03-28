"""PCM file browser – bottom-pane list TUI."""

import fcntl
import math
import os
import re
import signal
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


# ─── Low-level I/O helpers ───────────────────────────────────────────────────
# All reads use os.read(fd) directly to avoid TextIOWrapper buffering
# conflicts with the CPR escape sequence used for absolute positioning.

def _read_char(fd: int) -> str:
    """Read one Unicode character from a raw fd, handling multi-byte UTF-8."""
    b = os.read(fd, 1)
    if not b:
        return ''
    first = b[0]
    if first < 0x80:
        return chr(first)
    elif first < 0xE0:
        n_extra = 1
    elif first < 0xF0:
        n_extra = 2
    else:
        n_extra = 3
    for _ in range(n_extra):
        extra = os.read(fd, 1)
        if extra:
            b += extra
    return b.decode('utf-8', errors='replace')


def _query_cursor_row(fd: int) -> int:
    """Return the terminal's current cursor row (1-based) via CPR (ESC[6n).

    Returns -1 if the terminal does not respond within the timeout."""
    sys.stdout.write('\033[6n')
    sys.stdout.flush()
    buf = b''
    for _ in range(64):
        r, _, _ = select.select([fd], [], [], 0.3)
        if not r:
            break
        buf += os.read(fd, 1)
        if buf.endswith(b'R'):
            break
    m = re.search(rb'\x1b\[(\d+);(\d+)R', buf)
    return int(m.group(1)) if m else -1


# ─── Key reader ──────────────────────────────────────────────────────────────

def read_key(fd: int) -> str:
    ch = _read_char(fd)
    if ch == '\x1b':
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            ch2 = _read_char(fd)
            if ch2 == '[':
                r, _, _ = select.select([fd], [], [], 0.05)
                if r:
                    ch3 = _read_char(fd)
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

    Cursor positioning uses an absolute row obtained via CPR (ESC[6n) so that
    accidental cursor movement cannot drift the redraw position.

    Returns the selected file path from run(), or None if the user quit.
    """

    def __init__(self, files: list, base_dir: str, use_color: bool,
                 initial_cursor: int = 0):
        self.files         = files
        self.base_dir      = base_dir
        self.use_color     = use_color
        self.cursor        = max(0, min(initial_cursor, len(files) - 1))
        # Centre the initial cursor in the visible window
        self.offset        = max(0, min(
            self.cursor - MAX_VISIBLE // 2,
            max(0, len(files) - MAX_VISIBLE),
        ))
        self.selected      = None
        self._drawn_width  = 0

    # ── rendering ────────────────────────────────────────────────────────────

    def _c(self, text: str, *codes) -> str:
        if not self.use_color:
            return text
        return ''.join(codes) + text + R

    def _render(self) -> list:
        w     = shutil.get_terminal_size().columns
        self._drawn_width = w
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
        hints = "[↑↓/j/k] nav  [↵] select  [q/ESC] quit  "
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

    def _goto_top(self, tui_row: int):
        if tui_row > 0:
            sys.stdout.write(f'\033[{tui_row};1H')
        else:
            sys.stdout.write(cuu(TUI_H))

    # ── event loop ───────────────────────────────────────────────────────────

    def run(self):
        # Reserve TUI_H lines below current output without clearing it
        sys.stdout.write('\n' * TUI_H + cuu(TUI_H))
        sys.stdout.flush()
        self._draw(self._render())

        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tui_row = -1

        # Self-pipe: SIGWINCH writes a byte here so select() wakes immediately.
        sig_r, sig_w = os.pipe()
        fl = fcntl.fcntl(sig_w, fcntl.F_GETFL)
        fcntl.fcntl(sig_w, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        old_wakeup   = signal.set_wakeup_fd(sig_w)
        old_sigwinch = signal.signal(signal.SIGWINCH, lambda *_: None)

        try:
            tty.setraw(fd)

            # Anchor TUI position via CPR: cursor is now at TUI_top + TUI_H
            bottom = _query_cursor_row(fd)
            if bottom > 0:
                tui_row = bottom - TUI_H

            while True:
                # Block until key input OR terminal resize signal
                readable, _, _ = select.select([fd, sig_r], [], [])

                if sig_r in readable:
                    # Drain the pipe (may hold multiple signal bytes)
                    try:
                        os.read(sig_r, 256)
                    except OSError:
                        pass
                    # Compute how many terminal rows the old content now
                    # occupies after resize.  Each old line of width old_w
                    # wraps to ceil(old_w / new_w) rows at the new width.
                    old_w = self._drawn_width or shutil.get_terminal_size().columns
                    new_w = shutil.get_terminal_size().columns
                    if new_w > 0 and old_w > new_w:
                        wrap_factor = math.ceil(old_w / new_w)
                    else:
                        wrap_factor = 1
                    clear_lines = TUI_H * wrap_factor
                    self._goto_top(tui_row)
                    for _ in range(clear_lines):
                        sys.stdout.write('\r\033[2K\n')
                    self._goto_top(tui_row)
                    self._draw(self._render())
                    continue

                key = read_key(fd)

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

                self._goto_top(tui_row)
                self._draw(self._render())

        finally:
            signal.set_wakeup_fd(old_wakeup)
            signal.signal(signal.SIGWINCH, old_sigwinch)
            os.close(sig_r)
            os.close(sig_w)
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

        # Clear TUI area and leave cursor at TUI start
        self._goto_top(tui_row)
        for _ in range(TUI_H):
            sys.stdout.write('\r' + el() + '\n')
        self._goto_top(tui_row)
        sys.stdout.flush()

        return self.selected
