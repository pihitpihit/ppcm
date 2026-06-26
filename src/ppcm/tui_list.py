"""PCM file browser – bottom-pane list TUI."""

import fcntl
import io
import math
import os
import re
import signal
import subprocess
import sys
import shutil
import tempfile
import termios
import time
import tty
import wave
import select

from .ansi import (
    cuu, el, sgr,
    R, SEL, ROW_EVEN, ROW_ODD, BORDER, STATUS, COLHDR,
)
from .pcm_utils import (
    pcm_duration, fmt_size,
    PCM_SAMPLE_RATE, PCM_CHANNELS, PCM_BYTES_PER_SAMPLE,
)

MAX_VISIBLE = 15
# top-border + status + col-header + MAX_VISIBLE rows + bottom-border
TUI_H = 3 + MAX_VISIBLE + 1

# ─── Instant-play colour constants ───────────────────────────────────────────
# Foreground on filled (progress) portion  /  foreground on unfilled portion
_IP_PLAY_FILL   = sgr(38, 5, 0,   48, 5, 114)   # black on green
_IP_PLAY_EMPTY  = sgr(38, 5, 114, 48, 5, 0)     # green on black  (default bg)
_IP_PAUSE_FILL  = sgr(38, 5, 0,   48, 5, 214)   # black on orange
_IP_PAUSE_EMPTY = sgr(38, 5, 214, 48, 5, 0)     # orange on black
_IP_DONE_FULL   = sgr(1,  38, 5, 255, 48, 5, 26)  # bold white on steel-blue (= SEL)

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
    if ch == ' ':          return 'SPACE'
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

        # ── instant-play state ────────────────────────────────────────────
        # state: None | 'playing' | 'paused' | 'done'
        self._ip_state      = None
        self._ip_idx        = -1       # file index being played/done
        self._ip_proc       = None     # afplay subprocess
        self._ip_tmpwav     = None     # NamedTemporaryFile handle
        self._ip_duration   = 0.0
        self._ip_start_time = 0.0     # wall-clock anchor
        self._ip_paused_total = 0.0   # cumulative paused seconds
        self._ip_pause_at   = 0.0
        self._ip_pause_pos  = 0.0

    # ── instant-play engine ───────────────────────────────────────────────────

    def _ip_start_from(self, pos: float):
        """Start afplay from *pos* seconds into the current cursor file."""
        fp = self.files[self._ip_idx]
        bps = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_BYTES_PER_SAMPLE
        byte_offset = int(pos * bps)
        byte_offset -= byte_offset % PCM_BYTES_PER_SAMPLE

        with open(fp, 'rb') as f:
            f.seek(byte_offset)
            pcm_data = f.read()

        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(PCM_CHANNELS)
            wf.setsampwidth(PCM_BYTES_PER_SAMPLE)
            wf.setframerate(PCM_SAMPLE_RATE)
            wf.writeframes(pcm_data)

        if self._ip_tmpwav is not None:
            try:
                os.unlink(self._ip_tmpwav)
            except OSError:
                pass

        fd_tmp, tmp_path = tempfile.mkstemp(suffix='.wav')
        os.write(fd_tmp, buf.getvalue())
        os.close(fd_tmp)
        self._ip_tmpwav = tmp_path

        self._ip_proc = subprocess.Popen(
            ['afplay', tmp_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._ip_start_time   = time.time() - pos - self._ip_paused_total
        self._ip_state        = 'playing'

    def _ip_start(self):
        """Begin instant play from the beginning of the cursor file."""
        fp = self.files[self.cursor]
        self._ip_idx          = self.cursor
        self._ip_duration     = pcm_duration(fp)
        self._ip_paused_total = 0.0
        self._ip_pause_at     = 0.0
        self._ip_pause_pos    = 0.0
        self._ip_start_from(0.0)

    def _ip_pause(self):
        if self._ip_state != 'playing':
            return
        self._ip_pause_pos = self._ip_elapsed()
        self._ip_pause_at  = time.time()
        if self._ip_proc and self._ip_proc.poll() is None:
            self._ip_proc.terminate()
        self._ip_state = 'paused'

    def _ip_resume(self):
        if self._ip_state != 'paused':
            return
        self._ip_paused_total += time.time() - self._ip_pause_at
        self._ip_start_from(self._ip_pause_pos)

    def _ip_stop(self):
        """Kill playback without entering done state (cleanup)."""
        if self._ip_proc and self._ip_proc.poll() is None:
            self._ip_proc.terminate()
        self._ip_proc  = None
        self._ip_state = None
        if self._ip_tmpwav is not None:
            try:
                os.unlink(self._ip_tmpwav)
            except OSError:
                pass
            self._ip_tmpwav = None

    def _ip_stop_to_done(self):
        """Stop playback and enter done state for the current ip_idx."""
        if self._ip_proc and self._ip_proc.poll() is None:
            self._ip_proc.terminate()
        self._ip_proc  = None
        self._ip_state = 'done'
        if self._ip_tmpwav is not None:
            try:
                os.unlink(self._ip_tmpwav)
            except OSError:
                pass
            self._ip_tmpwav = None

    def _ip_elapsed(self) -> float:
        if self._ip_state == 'paused':
            return self._ip_pause_pos
        elapsed = time.time() - self._ip_start_time - self._ip_paused_total
        return min(elapsed, self._ip_duration)

    def _ip_proc_done(self) -> bool:
        """True when the afplay process has finished naturally."""
        if self._ip_state != 'playing':
            return False
        elapsed = time.time() - self._ip_start_time - self._ip_paused_total
        if elapsed >= self._ip_duration:
            return True
        return self._ip_proc is not None and self._ip_proc.poll() is not None

    # ── rendering ────────────────────────────────────────────────────────────

    def _c(self, text: str, *codes) -> str:
        if not self.use_color:
            return text
        return ''.join(codes) + text + R

    def _render_ip_row(self, row_text: str, w: int) -> str:
        """Render a row with instant-play progress fill overlay."""
        if self._ip_state == 'done':
            return _IP_DONE_FULL + row_text[:w].ljust(w) + R

        elapsed  = self._ip_elapsed()
        frac     = min(elapsed / self._ip_duration, 1.0) if self._ip_duration > 0 else 0.0
        fill_w   = int(frac * w)
        text     = row_text[:w].ljust(w)

        if self._ip_state == 'playing':
            fill_col  = _IP_PLAY_FILL
            empty_col = _IP_PLAY_EMPTY
        else:  # paused
            fill_col  = _IP_PAUSE_FILL
            empty_col = _IP_PAUSE_EMPTY

        filled  = fill_col  + text[:fill_w] + R
        unfilled = empty_col + text[fill_w:] + R
        return filled + unfilled

    def _render(self) -> list:
        w     = shutil.get_terminal_size().columns
        self._drawn_width = w
        total = len(self.files)
        lines = []

        # top border
        title = "  PCM Browser  "
        side  = (w - len(title)) // 2
        extra = w - len(title) - side * 2
        top   = ("─" * side + title + "─" * (side + extra))[:w]
        lines.append(self._c(top.ljust(w), BORDER))

        # status / key hints
        total_w = len(str(total))
        pos   = f"  {self.cursor + 1:>{total_w}}/{total}"
        if self._ip_state == 'playing':
            hints = "[↑↓/j/k] nav  [SPACE] pause  [↵] open  [q/ESC] quit  "
        elif self._ip_state == 'paused':
            hints = "[↑↓/j/k] nav  [SPACE] resume [↵] open  [q/ESC] quit  "
        else:
            hints = "[↑↓/j/k] nav  [SPACE] play   [↵] open  [q/ESC] quit  "
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
            is_ip  = (idx == self._ip_idx) and (self._ip_state is not None)

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

            if is_ip and self.use_color:
                lines.append(self._render_ip_row(row, w))
            elif is_cur:
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
                # Use short timeout when playing so progress updates smoothly
                timeout = 0.02 if self._ip_state == 'playing' else None
                readable, _, _ = select.select([fd, sig_r], [], [], timeout)

                # ── natural end detection ─────────────────────────────────
                if self._ip_proc_done():
                    self._ip_stop_to_done()
                    self._goto_top(tui_row)
                    self._draw(self._render())

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

                if fd not in readable:
                    # timeout fired (playing progress tick)
                    self._goto_top(tui_row)
                    self._draw(self._render())
                    continue

                key = read_key(fd)

                if key in ('q', 'Q', 'ESC', 'CTRL_C'):
                    break

                elif key == 'SPACE':
                    if self._ip_state is None or self._ip_state == 'done':
                        # start fresh on current cursor
                        self._ip_stop()
                        self._ip_start()
                    elif self._ip_state == 'playing':
                        self._ip_pause()
                    elif self._ip_state == 'paused':
                        self._ip_resume()

                elif key in ('k', 'UP'):
                    if self._ip_state is not None:
                        self._ip_stop()
                    self.cursor = (self.cursor - 1) % len(self.files)
                    if self.cursor < self.offset:
                        self.offset = self.cursor
                    elif self.cursor == len(self.files) - 1:
                        self.offset = max(0, len(self.files) - MAX_VISIBLE)

                elif key in ('j', 'DOWN'):
                    if self._ip_state is not None:
                        self._ip_stop()
                    self.cursor = (self.cursor + 1) % len(self.files)
                    if self.cursor == 0:
                        self.offset = 0
                    elif self.cursor >= self.offset + MAX_VISIBLE:
                        self.offset = self.cursor - MAX_VISIBLE + 1

                elif key == 'ENTER':
                    self._ip_stop()
                    self.selected = self.files[self.cursor]
                    break

                self._goto_top(tui_row)
                self._draw(self._render())

        finally:
            self._ip_stop()
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
