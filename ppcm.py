#!/usr/bin/env python3
"""ppcm - PCM audio file player with TUI interface"""

import sys
import os
import argparse
import glob
import termios
import tty
import select
import shutil

VERSION = "0.1.0"

# PCM defaults: 16-bit, 22050Hz, mono
PCM_SAMPLE_RATE = 22050
PCM_CHANNELS = 1
PCM_BYTES_PER_SAMPLE = 2


# ─── ANSI helpers ────────────────────────────────────────────────────────────

def cuu(n):   return f'\033[{n}A'        # cursor up n lines
def el():     return '\033[K'            # erase to end of line
def sgr(*c):  return f'\033[{";".join(str(x) for x in c)}m'

# Palette (256-colour)
R   = sgr(0)
SEL = sgr(1, 38, 5, 255, 48, 5, 26)   # bold white on steel-blue
ROW_EVEN = sgr(38, 5, 252)
ROW_ODD  = sgr(38, 5, 244)
BORDER   = sgr(38, 5, 74)             # sky-blue
STATUS   = sgr(38, 5, 245)            # medium-gray
COLHDR   = sgr(1, 38, 5, 255, 48, 5, 237)  # bold white on very-dark
FOOTER   = sgr(38, 5, 240, 48, 5, 232)


def supports_color() -> bool:
    return sys.stdout.isatty()


# ─── PCM utilities ───────────────────────────────────────────────────────────

def pcm_duration(path: str) -> float:
    size = os.path.getsize(path)
    bps  = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_BYTES_PER_SAMPLE
    return size / bps


def fmt_size(n: int) -> str:
    if n < 1024:      return f"{n}B"
    if n < 1024**2:   return f"{n/1024:.1f}K"
    return             f"{n/1024**2:.2f}M"


def scan_pcm(directory: str) -> list[str]:
    pattern = os.path.join(directory, '**', '*.pcm')
    return sorted(glob.glob(pattern, recursive=True))


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
                    return {'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT'}.get(ch3, f'ESC[{ch3}')
        return 'ESC'
    if ch in ('\r', '\n'): return 'ENTER'
    if ch == '\x03':       return 'CTRL_C'
    return ch


# ─── List TUI ────────────────────────────────────────────────────────────────

MAX_VISIBLE = 15
# lines: top-border + status + col-header + MAX_VISIBLE entries + bottom-border
TUI_H = 3 + MAX_VISIBLE + 1


class ListTUI:
    def __init__(self, files: list[str], base_dir: str, use_color: bool):
        self.files    = files
        self.base_dir = base_dir
        self.use_color = use_color
        self.cursor   = 0
        self.offset   = 0
        self.selected = None

    def _c(self, text: str, *codes) -> str:
        if not self.use_color:
            return text
        return ''.join(codes) + text + R

    def _render(self) -> list[str]:
        w      = shutil.get_terminal_size().columns
        total  = len(self.files)
        lines  = []

        # ── top border ──
        title  = f"  PCM Browser  "
        side   = (w - len(title) - 2) // 2
        extra  = w - len(title) - 2 - side * 2
        top    = ("─" * side + title + "─" * (side + extra))[:w]
        lines.append(self._c(top.ljust(w), BORDER))

        # ── status / key hints ──
        pos   = f"  {self.cursor+1}/{total}"
        hints = "[j/k/↑↓] nav  [↵] select  [q/ESC] quit  "
        pad   = max(w - len(pos) - len(hints), 1)
        lines.append(self._c((pos + " " * pad + hints)[:w].ljust(w), STATUS))

        # ── column header ──
        path_w  = max(w - 22, 10)
        col_hdr = f"  {'FILE':<{path_w}}{'SIZE':>8}{'DURATION':>12}"
        lines.append(self._c(col_hdr[:w].ljust(w), COLHDR))

        # ── file entries ──
        visible = self.files[self.offset: self.offset + MAX_VISIBLE]
        for i, fp in enumerate(visible):
            idx    = self.offset + i
            is_cur = (idx == self.cursor)

            try:
                sz  = os.path.getsize(fp)
                dur = pcm_duration(fp)
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

        # ── bottom border ──
        lines.append(self._c(("─" * w)[:w], BORDER))

        return lines

    def _draw(self, lines: list[str]):
        buf = []
        for ln in lines:
            buf.append('\r' + ln + el() + '\n')
        sys.stdout.write(''.join(buf))
        sys.stdout.flush()

    def run(self):
        # reserve space
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

        # clear TUI area
        sys.stdout.write(cuu(TUI_H))
        for _ in range(TUI_H):
            sys.stdout.write('\r' + el() + '\n')
        sys.stdout.write(cuu(TUI_H))
        sys.stdout.flush()

        return self.selected


# ─── Colored help formatter ──────────────────────────────────────────────────

def _colorize_help(text: str) -> str:
    import re

    out = []
    for line in text.split('\n'):

        # "usage: ppcm [-h] [--version] [PATH]"
        if line.startswith('usage:'):
            rest = line[6:]
            # brackets first (clean text – no ANSI interference)
            rest = re.sub(r'(\[.*?\])', sgr(38, 5, 180) + r'\1' + R, rest)
            # prog name (still uncolored at this point)
            rest = re.sub(r'\bppcm\b', sgr(1, 38, 5, 255) + 'ppcm' + R, rest, count=1)
            line = sgr(1, 38, 5, 74) + 'usage:' + R + rest

        # section headers:  "positional arguments:"  "options:"
        elif re.match(r'^[a-zA-Z][a-zA-Z ]*:$', line):
            line = sgr(1, 38, 5, 74) + line + R

        # argument/option rows  "  PATH  …"  "  -h, --help  …"
        # (lowercase lines like "  ppcm …" fall through to the epilog handler)
        elif re.match(r'^  (-|[A-Z])', line):
            m = re.search(r'  +\S', line[2:])
            if m:
                split   = 2 + m.start()
                opt_raw = line[:split]
                hlp_raw = line[split:]
                opt_raw = re.sub(r'(-{1,2}[\w-]+)', sgr(38, 5, 222) + r'\1' + R, opt_raw)
                opt_raw = re.sub(r'\b([A-Z]{2,})\b',  sgr(38, 5, 116) + r'\1' + R, opt_raw)
                line = opt_raw + sgr(38, 5, 245) + hlp_raw + R
            else:
                line = re.sub(r'(-{1,2}[\w-]+)', sgr(38, 5, 222) + r'\1' + R, line)
                line = re.sub(r'\b([A-Z]{2,})\b',  sgr(38, 5, 116) + r'\1' + R, line)

        # epilog example lines  "  ppcm ./audio/    scan …"
        elif re.match(r'^  ppcm\b', line):
            m = re.match(r'^(  ppcm.*?)(\s{2,})(.+)$', line)
            if m:
                cmd, spc, desc = m.groups()
                cmd  = re.sub(r'\bppcm\b', sgr(38, 5, 114) + 'ppcm' + R, cmd, count=1)
                line = cmd + spc + sgr(38, 5, 245) + desc + R
            else:
                line = re.sub(r'\bppcm\b', sgr(38, 5, 114) + 'ppcm' + R, line, count=1)

        out.append(line)

    return '\n'.join(out)


class _ColorHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def format_help(self) -> str:
        text = super().format_help()
        if supports_color():
            text = _colorize_help(text)
        return text


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog='ppcm',
        description='PCM audio file player',
        formatter_class=_ColorHelpFormatter,
        epilog="""\
examples:
  ppcm ./audio/          scan directory and browse PCM files
  ppcm sample.pcm        open PCM file directly
  ppcm --version         show version
""",
    )
    parser.add_argument('path', nargs='?', metavar='PATH',
                        help='PCM file or directory to scan')
    parser.add_argument('--version', action='store_true',
                        help='show version and exit')

    args = parser.parse_args()

    if args.version:
        print(f"ppcm {VERSION}")
        return

    if args.path is None:
        parser.print_help()
        return

    path = os.path.abspath(args.path)

    if os.path.isdir(path):
        files = scan_pcm(path)
        if not files:
            print(f"ppcm: no PCM files found under '{args.path}'", file=sys.stderr)
            sys.exit(1)

        tui      = ListTUI(files, base_dir=path, use_color=supports_color())
        selected = tui.run()

        if selected:
            # play screen – placeholder until play spec is defined
            print(f"selected: {selected}")

    elif os.path.isfile(path):
        # direct file – play screen placeholder
        print(f"playing: {path}")

    else:
        print(f"ppcm: '{args.path}': no such file or directory", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
