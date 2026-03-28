"""PCM player – bottom-pane play TUI."""

import fcntl
import io
import math
import os
import select
import shutil
import signal
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

TUI_H = 8

# Sub-character block elements: index = eighths filled (0–8)
_BLOCKS = [' ', '▏', '▎', '▍', '▌', '▋', '▊', '▉', '█']

# Additional palette entries for play screen
_PLAY   = sgr(38, 5, 114)       # green  – playing indicator
_PAUSE  = sgr(38, 5, 222)       # gold   – paused indicator
_DONE   = sgr(38, 5, 74)        # sky-blue – done indicator
_BAR_ON = sgr(38, 5, 74)        # filled bar colour
_BAR_OF = sgr(38, 5, 238)       # empty bar colour



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
        self._drawn_width  = 0

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
        if self._start_time is None:
            return 0.0
        paused = self._paused_total
        if self._paused and self._pause_at:
            paused += time.time() - self._pause_at
        t = time.time() - self._start_time - paused
        return max(0.0, min(t, self.duration))

    def _is_done(self) -> bool:
        return (
            (self._proc is not None and self._proc.poll() is not None)
            or self._elapsed() >= self.duration
        )

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

        # blank
        lines.append(' ' * w)

        # progress bar – sub-character precision via 1/8-block elements
        bar_w   = max(w - 20, 4)
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
        hints = ('  [SPACE] replay  [q/ESC] back  ' if done
                 else '  [SPACE] pause/resume  [q/ESC] back  ')
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
