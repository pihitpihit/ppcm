"""PCM player – bottom-pane play TUI."""

import fcntl
import io
import math
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
import wave

from .ansi import (
    cuu, el, sgr,
    R, BORDER, STATUS, COLHDR,
)
from .pcm_utils import (
    pcm_duration, fmt_size,
    PCM_SAMPLE_RATE, PCM_CHANNELS, PCM_BYTES_PER_SAMPLE,
)
from .tui_list import _query_cursor_row, read_key

_WAVE_H = 8   # terminal lines for waveform

# Sub-character block elements: index = eighths filled (0–8)
_BLOCKS = [' ', '▏', '▎', '▍', '▌', '▋', '▊', '▉', '█']

# Braille dot-bit table: _DOT_BITS[dot_row][dot_col]
_DOT_BITS = [
    [0x01, 0x08],
    [0x02, 0x10],
    [0x04, 0x20],
    [0x40, 0x80],
]

# Palette
_BAR_ON       = sgr(38, 5, 74)               # sky-blue  – filled bar / waveform
_BAR_OF       = sgr(38, 5, 238)              # dark gray – empty bar
_PLAYHEAD     = sgr(1, 38, 5, 255)           # bold white – playhead column
_WAVE_BORDER  = sgr(38, 5, 68)              # medium blue – inner border
_STATE_READY  = sgr(38, 5, 252, 48, 5, 237) # light on dark-gray
_STATE_PLAY   = sgr(38, 5, 0,   48, 5, 114) # black on green
_STATE_PAUSE  = sgr(38, 5, 0,   48, 5, 214) # black on orange
_STATE_DONE   = sgr(38, 5, 0,   48, 5, 74)  # black on sky-blue


class PlayTUI:
    """Bottom-pane PCM player TUI.

    Starts in READY state (no auto-play). SPACE begins playback.
    Keys: SPACE play/pause/resume/replay · h/← -0.1s · l/→ +0.1s · q/ESC back.
    """

    def __init__(self, path: str, use_color: bool):
        self.path          = path
        self.use_color     = use_color
        self.duration      = pcm_duration(path)
        self.size          = os.path.getsize(path)

        self._proc           = None
        self._tmpwav         = None
        self._paused         = False
        self._pause_pos      = 0.0
        self._start_time     = None
        self._pause_at       = None
        self._paused_total   = 0.0
        self._drawn_width    = 0
        self._tui_h          = 16      # updated by _render(); 16 = max possible
        self._waveform_cache = (0, [])

    # ── state queries ─────────────────────────────────────────────────────────

    def _is_ready(self) -> bool:
        return self._proc is None and self._start_time is None and not self._paused

    def _is_done(self) -> bool:
        if self._paused:
            return False
        return (
            (self._proc is not None and self._proc.poll() is not None)
            or self._elapsed() >= self.duration
        )

    # ── playback ─────────────────────────────────────────────────────────────

    def _start(self):
        self._start_from(0.0)

    def _start_from(self, pos: float):
        with open(self.path, 'rb') as f:
            pcm_data = f.read()
        bps    = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_BYTES_PER_SAMPLE
        offset = int(pos * bps)
        offset = (offset // PCM_BYTES_PER_SAMPLE) * PCM_BYTES_PER_SAMPLE

        buf = io.BytesIO()
        with wave.open(buf, 'wb') as w:
            w.setnchannels(PCM_CHANNELS)
            w.setsampwidth(PCM_BYTES_PER_SAMPLE)
            w.setframerate(PCM_SAMPLE_RATE)
            w.writeframes(pcm_data[offset:])

        if self._tmpwav:
            try:
                os.unlink(self._tmpwav)
            except OSError:
                pass
        tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        tmp.write(buf.getvalue())
        tmp.close()
        self._tmpwav = tmp.name
        self._proc = subprocess.Popen(
            ['afplay', self._tmpwav],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._start_time = time.time() - pos - self._paused_total

    def _pause(self):
        if self._paused or self._is_done() or self._is_ready():
            return
        self._pause_pos = self._elapsed()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._paused   = True
        self._pause_at = time.time()

    def _resume(self):
        if not self._paused:
            return
        if self._pause_at is not None:
            self._paused_total += time.time() - self._pause_at
        self._pause_at = None
        self._paused   = False
        self._start_from(self._pause_pos)

    def _seek(self, delta: float):
        if self._is_ready():
            return
        if self._paused:
            self._pause_pos = max(0.0, min(self._pause_pos + delta, self.duration))
        elif self._is_done():
            new_pos = max(0.0, min(self.duration + delta, self.duration))
            if new_pos < self.duration:
                self._paused    = True
                self._pause_pos = new_pos
                self._pause_at  = None
        else:
            new_pos = max(0.0, min(self._elapsed() + delta, self.duration))
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._start_from(new_pos)

    def _restart(self):
        self._stop()
        self._paused       = False
        self._pause_pos    = 0.0
        self._start_time   = None
        self._pause_at     = None
        self._paused_total = 0.0
        self._start()

    def _stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self._tmpwav:
            try:
                os.unlink(self._tmpwav)
            except OSError:
                pass
            self._tmpwav = None

    def _elapsed(self) -> float:
        if self._paused:
            return self._pause_pos
        if self._start_time is None:
            return 0.0
        t = time.time() - self._start_time - self._paused_total
        return max(0.0, min(t, self.duration))

    # ── waveform ──────────────────────────────────────────────────────────────

    def _compute_waveform(self, n_dot_cols: int) -> list:
        with open(self.path, 'rb') as f:
            raw = f.read()
        n_samples = len(raw) // PCM_BYTES_PER_SAMPLE
        if n_samples == 0:
            return [0.0] * n_dot_cols
        amplitudes = []
        for col in range(n_dot_cols):
            s = col * n_samples // n_dot_cols
            e = max(s + 1, (col + 1) * n_samples // n_dot_cols)
            e = min(e, n_samples)
            chunk = struct.unpack_from(f'<{e - s}h', raw, s * PCM_BYTES_PER_SAMPLE)
            amplitudes.append(max(abs(v) for v in chunk) / 32767.0)
        return amplitudes

    def _get_waveform(self, n_dot_cols: int) -> list:
        if self._waveform_cache[0] != n_dot_cols:
            self._waveform_cache = (n_dot_cols, self._compute_waveform(n_dot_cols))
        return self._waveform_cache[1]

    def _render_waveform(self, bar_w: int, elapsed: float) -> list:
        n_dot_cols = bar_w * 2
        n_dot_rows = _WAVE_H * 4
        amplitudes = self._get_waveform(n_dot_cols)

        grid = [[False] * n_dot_cols for _ in range(n_dot_rows)]
        for col, amp in enumerate(amplitudes):
            h = min(int(amp * n_dot_rows), n_dot_rows)
            for row in range(n_dot_rows - h, n_dot_rows):
                grid[row][col] = True

        if self.duration > 0:
            ph = min(int(elapsed / self.duration * n_dot_cols) // 2, bar_w - 1)
        else:
            ph = -1

        lines = []
        for line_r in range(_WAVE_H):
            chars = []
            for cc in range(bar_w):
                bits = 0
                for dr in range(4):
                    for dc in range(2):
                        if grid[line_r * 4 + dr][cc * 2 + dc]:
                            bits |= _DOT_BITS[dr][dc]
                ch = chr(0x2800 + bits)
                if self.use_color:
                    chars.append((_PLAYHEAD if cc == ph else _BAR_ON) + ch + R)
                else:
                    chars.append(ch)
            lines.append(''.join(chars))
        return lines

    # ── rendering ─────────────────────────────────────────────────────────────

    def _c(self, text: str, *codes) -> str:
        if not self.use_color:
            return text
        return ''.join(codes) + text + R

    def _box_line(self, content: str, content_vis_w: int, inner_w: int) -> str:
        """Border-wrapped line.  *content* may contain ANSI codes;
        *content_vis_w* is its visible character width."""
        pad = max(0, inner_w - content_vis_w)
        if self.use_color:
            return (_WAVE_BORDER + '  │' + R
                    + content + ' ' * pad
                    + _WAVE_BORDER + '│' + R)
        return '  │' + content + ' ' * pad + '│'

    def _render(self) -> list:
        w       = shutil.get_terminal_size().columns
        self._drawn_width = w
        elapsed = self._elapsed()
        done    = self._is_done()
        ready   = self._is_ready()
        lines   = []

        # ── dimensions ──────────────────────────────────────────────────────
        dur_str     = f'{self.duration:.3f}'
        dur_str_w   = len(dur_str)
        # visible width of the time part inside the border:
        #   '  ' + elapsed.rjust(dw) + ' / ' + dur_str + 's'
        time_part_w = 2 + dur_str_w + 3 + dur_str_w + 1   # = 2*dw + 6
        # inner border width: w - 4  (for '  │' + content + '│')
        bar_w   = max(w - 4 - time_part_w, 4)
        inner_w = bar_w + time_part_w                      # = w - 4

        # ── header border ────────────────────────────────────────────────────
        title = '  PCM Player  '
        side  = (w - len(title) - 2) // 2
        extra = w - len(title) - 2 - side * 2
        lines.append(self._c(
            ('─' * side + title + '─' * (side + extra))[:w].ljust(w), BORDER))

        # ── file info (1 or 2 lines) ─────────────────────────────────────────
        name       = os.path.basename(self.path)
        right_part = f'{self.duration:.3f}s · {fmt_size(self.size)}'
        if 2 + len(name) + 1 + len(right_part) <= w:
            gap = w - 2 - len(name) - len(right_part)
            lines.append(self._c(f'  {name}' + ' ' * gap + right_part, COLHDR))
        else:
            lines.append(self._c(f'  {name[:w-2]}'.ljust(w), COLHDR))
            lines.append(self._c(right_part[:w].rjust(w), STATUS))

        # ── state + hints (1 or 2 lines) ─────────────────────────────────────
        if ready:
            state_txt, state_clr = ' READY ',   _STATE_READY
            hints_txt = '[SPACE] play  [q/ESC] back  '
        elif done:
            state_txt, state_clr = ' DONE ',    _STATE_DONE
            hints_txt = '[h/←] -0.1s  [l/→] +0.1s  [SPACE] replay  [q/ESC] back  '
        elif self._paused:
            state_txt, state_clr = ' PAUSED ',  _STATE_PAUSE
            hints_txt = '[h/←] -0.1s  [l/→] +0.1s  [SPACE] resume  [q/ESC] back  '
        else:
            state_txt, state_clr = ' PLAYING ', _STATE_PLAY
            hints_txt = '[h/←] -0.1s  [l/→] +0.1s  [SPACE] pause  [q/ESC] back  '

        s_col = self._c(state_txt, state_clr)
        h_col = self._c(hints_txt, STATUS)

        if len(state_txt) + len(hints_txt) <= w:
            gap = w - len(state_txt) - len(hints_txt)
            lines.append(s_col + ' ' * gap + h_col if self.use_color
                         else state_txt + ' ' * gap + hints_txt)
        else:
            lines.append(s_col + ' ' * (w - len(state_txt)) if self.use_color
                         else state_txt.ljust(w))
            lines.append(' ' * (w - len(hints_txt)) + h_col if self.use_color
                         else hints_txt.rjust(w))

        # ── waveform + progress (bordered section) ────────────────────────────
        dash = '─' * inner_w
        lines.append(self._c(f'  ┌{dash}┐', _WAVE_BORDER))

        for wf_line in self._render_waveform(bar_w, elapsed):
            lines.append(self._box_line(wf_line, bar_w, inner_w))

        # progress bar (no brackets)
        ratio   = (elapsed / self.duration) if self.duration > 0 else 0.0
        eighths = int(ratio * bar_w * 8)
        full    = eighths // 8
        partial = eighths % 8
        empty   = bar_w - full - (1 if partial else 0)

        elapsed_str = f'{elapsed:.3f}'.rjust(dur_str_w)
        time_str    = f'  {elapsed_str} / {dur_str}s'

        if self.use_color:
            bar  = (_BAR_ON + '█' * full
                    + (_BLOCKS[partial] if partial else '')
                    + R + _BAR_OF + ' ' * empty + R)
            prog = bar + time_str
        else:
            bar  = '█' * full + (_BLOCKS[partial] if partial else '') + ' ' * empty
            prog = bar + time_str
        lines.append(self._box_line(prog, inner_w, inner_w))

        lines.append(self._c(f'  └{dash}┘', _WAVE_BORDER))

        self._tui_h = len(lines)
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
            sys.stdout.write(cuu(self._tui_h))

    # ── event loop ────────────────────────────────────────────────────────────

    def run(self):
        # Render first to determine actual height before reserving lines
        initial_lines = self._render()
        sys.stdout.write('\n' * self._tui_h + cuu(self._tui_h))
        sys.stdout.flush()
        self._draw(initial_lines)

        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tui_row = -1

        sig_r, sig_w = os.pipe()
        fl = fcntl.fcntl(sig_w, fcntl.F_GETFL)
        fcntl.fcntl(sig_w, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        old_wakeup   = signal.set_wakeup_fd(sig_w)
        old_sigwinch = signal.signal(signal.SIGWINCH, lambda *_: None)

        try:
            tty.setraw(fd)
            bottom = _query_cursor_row(fd)
            if bottom > 0:
                tui_row = bottom - self._tui_h

            # Player starts in READY state – no auto-play

            while True:
                readable, _, _ = select.select([fd, sig_r], [], [], 0.02)

                if sig_r in readable:
                    try:
                        os.read(sig_r, 256)
                    except OSError:
                        pass
                    old_w = self._drawn_width or shutil.get_terminal_size().columns
                    old_h = self._tui_h
                    new_w = shutil.get_terminal_size().columns
                    wrap_factor = math.ceil(old_w / new_w) if new_w > 0 and old_w > new_w else 1
                    self._goto_top(tui_row)
                    for _ in range(old_h * wrap_factor):
                        sys.stdout.write('\r\033[2K\n')
                    self._goto_top(tui_row)
                    self._draw(self._render())
                    continue

                if fd in readable:
                    key = read_key(fd)
                    if key in ('q', 'Q', 'ESC', 'CTRL_C'):
                        break
                    elif key == ' ':
                        if self._is_ready():
                            self._start()
                        elif self._is_done():
                            self._restart()
                        elif self._paused:
                            self._resume()
                        else:
                            self._pause()
                    elif key in ('h', 'LEFT'):
                        self._seek(-0.1)
                    elif key in ('l', 'RIGHT'):
                        self._seek(0.1)

                self._goto_top(tui_row)
                self._draw(self._render())

        finally:
            self._stop()
            signal.set_wakeup_fd(old_wakeup)
            signal.signal(signal.SIGWINCH, old_sigwinch)
            os.close(sig_r)
            os.close(sig_w)
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

        # Clear TUI area
        self._goto_top(tui_row)
        for _ in range(self._tui_h):
            sys.stdout.write('\r' + el() + '\n')
        self._goto_top(tui_row)
        sys.stdout.flush()
