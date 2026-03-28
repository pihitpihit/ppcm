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

TUI_H      = 15   # 1 border + 1 name + 1 info + 8 waveform + 1 bar + 1 state + 1 hints + 1 border
_WAVE_H    = 8    # terminal lines occupied by the waveform

# Sub-character block elements: index = eighths filled (0–8)
_BLOCKS = [' ', '▏', '▎', '▍', '▌', '▋', '▊', '▉', '█']

# Braille dot-bit table: _DOT_BITS[dot_row][dot_col]
# Each Braille char is 2 dot-cols × 4 dot-rows.
_DOT_BITS = [
    [0x01, 0x08],   # dot-row 0  (top)
    [0x02, 0x10],   # dot-row 1
    [0x04, 0x20],   # dot-row 2
    [0x40, 0x80],   # dot-row 3  (bottom)
]

# Additional palette entries for play screen
_PLAY     = sgr(38, 5, 114)     # green     – playing indicator
_PAUSE    = sgr(38, 5, 222)     # gold      – paused indicator
_DONE     = sgr(38, 5, 74)      # sky-blue  – done indicator
_BAR_ON   = sgr(38, 5, 74)      # sky-blue  – filled bar / waveform
_BAR_OF   = sgr(38, 5, 238)     # dark gray – empty bar
_PLAYHEAD = sgr(1, 38, 5, 255)  # bold white – playhead column



class PlayTUI:
    """Bottom-pane PCM player TUI.

    Plays a single PCM file via afplay (macOS).
    Keys: SPACE – pause/resume · q/ESC – back to list.
    """

    def __init__(self, path: str, use_color: bool):
        self.path          = path
        self.use_color     = use_color
        self.duration      = pcm_duration(path)
        self.size          = os.path.getsize(path)

        self._proc         = None
        self._tmpwav       = None   # path of temp WAV file
        self._paused       = False
        self._pause_pos    = 0.0    # elapsed seconds when pause was triggered
        self._start_time   = None
        self._pause_at     = None   # time.time() when last pause began
        self._paused_total = 0.0    # total seconds spent paused
        self._drawn_width    = 0
        self._waveform_cache = (0, [])   # (n_dot_cols, amplitudes)

    # ── playback ─────────────────────────────────────────────────────────────

    def _start(self):
        self._start_from(0.0)

    def _start_from(self, pos: float):
        """Start afplay from *pos* seconds into the file.

        Adjusts _start_time so that _elapsed() returns *pos* immediately,
        ensuring the progress bar continues from the correct position.
        """
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
        # elapsed() = time.time() - _start_time - _paused_total  →  pos
        self._start_time = time.time() - pos - self._paused_total

    def _pause(self):
        if self._paused or self._is_done():
            return
        self._pause_pos = self._elapsed()
        # Terminate immediately to flush the OS audio buffer
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
        """Seek by *delta* seconds (negative = backward)."""
        if self._paused:
            self._pause_pos = max(0.0, min(self._pause_pos + delta, self.duration))
        elif self._is_done():
            new_pos = max(0.0, min(self.duration + delta, self.duration))
            if new_pos < self.duration:
                # Enter paused state at new position; SPACE resumes from here
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

    def _is_done(self) -> bool:
        if self._paused:
            return False
        return (
            (self._proc is not None and self._proc.poll() is not None)
            or self._elapsed() >= self.duration
        )

    # ── waveform ──────────────────────────────────────────────────────────────

    def _compute_waveform(self, n_dot_cols: int) -> list:
        """Return peak amplitude (0.0–1.0) for each of *n_dot_cols* columns."""
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
        """Return *_WAVE_H* terminal lines of Braille waveform with playhead."""
        n_dot_cols = bar_w * 2
        n_dot_rows = _WAVE_H * 4          # 32 dot-rows total
        amplitudes = self._get_waveform(n_dot_cols)

        # Build boolean dot grid: row 0 = top (high amp), row 31 = baseline
        # Fill from baseline upward for each column's amplitude.
        grid = [[False] * n_dot_cols for _ in range(n_dot_rows)]
        for col, amp in enumerate(amplitudes):
            h = min(int(amp * n_dot_rows), n_dot_rows)
            for row in range(n_dot_rows - h, n_dot_rows):
                grid[row][col] = True

        # Playhead: Braille char column corresponding to current position
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
                    if cc == ph:
                        chars.append(_PLAYHEAD + ch + R)
                    else:
                        chars.append(_BAR_ON + ch + R)
                else:
                    chars.append(ch)
            lines.append(''.join(chars))
        return lines

    # ── rendering ─────────────────────────────────────────────────────────────

    def _c(self, text: str, *codes) -> str:
        if not self.use_color:
            return text
        return ''.join(codes) + text + R

    def _render(self) -> list:
        w       = shutil.get_terminal_size().columns
        self._drawn_width = w
        elapsed = self._elapsed()
        done    = self._is_done()
        lines   = []

        # top border
        title = '  PCM Player  '
        side  = (w - len(title) - 2) // 2
        extra = w - len(title) - 2 - side * 2
        top   = ('─' * side + title + '─' * (side + extra))[:w]
        lines.append(self._c(top.ljust(w), BORDER))

        # filename
        name = os.path.basename(self.path)
        if len(name) > w - 4:
            name = '…' + name[-(w - 5):]
        lines.append(self._c(f'  {name}'.ljust(w), COLHDR))

        # duration + size
        info = f'  {self.duration:.3f}s · {fmt_size(self.size)}'
        lines.append(self._c(info[:w].ljust(w), STATUS))

        # waveform (8 lines) – aligned with bar interior (3-char indent)
        bar_w   = max(w - 20, 4)
        pad_r   = max(0, w - 3 - bar_w)
        for wf_line in self._render_waveform(bar_w, elapsed):
            lines.append('   ' + wf_line + ' ' * pad_r)

        # progress bar – sub-character precision via 1/8-block elements
        ratio   = (elapsed / self.duration) if self.duration > 0 else 0.0
        eighths = int(ratio * bar_w * 8)
        full    = eighths // 8
        partial = eighths % 8
        empty   = bar_w - full - (1 if partial else 0)

        time_str = f'{elapsed:.1f} / {self.duration:.1f}s'
        if self.use_color:
            bar = (
                _BAR_ON + '█' * full
                + (_BLOCKS[partial] if partial else '')
                + R
                + _BAR_OF + ' ' * empty + R
            )
            prog_line = f'  [{bar}]  {time_str}'
        else:
            bar = '█' * full + (_BLOCKS[partial] if partial else '') + ' ' * empty
            prog_line = (f'  [{bar}]  {time_str}')[:w].ljust(w)
        lines.append(prog_line.ljust(w) if not self.use_color else prog_line)

        # state indicator
        if done:
            state = '  [ DONE ]'
            lines.append(self._c(state[:w].ljust(w), _DONE))
        elif self._paused:
            state = '  [ PAUSED ]'
            lines.append(self._c(state[:w].ljust(w), _PAUSE))
        else:
            state = '  [ PLAYING ]'
            lines.append(self._c(state[:w].ljust(w), _PLAY))

        # key hints
        hints = ('  [SPACE] replay  [h/←] -0.1s  [l/→] +0.1s  [q/ESC] back  ' if done
                 else '  [SPACE] pause/resume  [h/←] -0.1s  [l/→] +0.1s  [q/ESC] back  ')
        lines.append(self._c(hints[:w].ljust(w), STATUS))

        # bottom border
        lines.append(self._c(('─' * w)[:w], BORDER))

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

    # ── event loop ────────────────────────────────────────────────────────────

    def run(self):
        sys.stdout.write('\n' * TUI_H + cuu(TUI_H))
        sys.stdout.flush()
        self._draw(self._render())

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
                tui_row = bottom - TUI_H

            self._start()

            while True:
                # ~50 fps timeout drives progress-bar updates
                readable, _, _ = select.select([fd, sig_r], [], [], 0.02)

                if sig_r in readable:
                    try:
                        os.read(sig_r, 256)
                    except OSError:
                        pass
                    old_w = self._drawn_width or shutil.get_terminal_size().columns
                    new_w = shutil.get_terminal_size().columns
                    wrap_factor = math.ceil(old_w / new_w) if new_w > 0 and old_w > new_w else 1
                    self._goto_top(tui_row)
                    for _ in range(TUI_H * wrap_factor):
                        sys.stdout.write('\r\033[2K\n')
                    self._goto_top(tui_row)
                    self._draw(self._render())
                    continue

                if fd in readable:
                    key = read_key(fd)
                    if key in ('q', 'Q', 'ESC', 'CTRL_C'):
                        break
                    elif key == ' ':
                        if self._is_done():
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

        # clear TUI area and leave cursor at TUI start
        self._goto_top(tui_row)
        for _ in range(TUI_H):
            sys.stdout.write('\r' + el() + '\n')
        self._goto_top(tui_row)
        sys.stdout.flush()
