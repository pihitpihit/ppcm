"""Colourised argparse help formatter."""

import re
import argparse

from ansi import (
    supports_color,
    R,
    H_LABEL, H_PROG, H_BRACK, H_OPT, H_META, H_HELP, H_CMD,
)


def _colorize(text: str) -> str:
    out = []
    for line in text.split('\n'):

        # "usage: ppcm [-h] [--version] [PATH]"
        if line.startswith('usage:'):
            rest = line[6:]
            # brackets first (clean text – avoids ANSI interference)
            rest = re.sub(r'(\[.*?\])', H_BRACK + r'\1' + R, rest)
            # prog name
            rest = re.sub(r'\bppcm\b', H_PROG + 'ppcm' + R, rest, count=1)
            line = H_LABEL + 'usage:' + R + rest

        # section headers: "positional arguments:"  "options:"
        elif re.match(r'^[a-zA-Z][a-zA-Z ]*:$', line):
            line = H_LABEL + line + R

        # argument/option rows: "  PATH …"  "  -h, --help …"
        # lowercase-starting lines (e.g. "  ppcm …") fall through to epilog handler
        elif re.match(r'^  (-|[A-Z])', line):
            m = re.search(r'  +\S', line[2:])
            if m:
                split   = 2 + m.start()
                opt_raw = line[:split]
                hlp_raw = line[split:]
                opt_raw = re.sub(r'(-{1,2}[\w-]+)', H_OPT  + r'\1' + R, opt_raw)
                opt_raw = re.sub(r'\b([A-Z]{2,})\b', H_META + r'\1' + R, opt_raw)
                line = opt_raw + H_HELP + hlp_raw + R
            else:
                line = re.sub(r'(-{1,2}[\w-]+)', H_OPT  + r'\1' + R, line)
                line = re.sub(r'\b([A-Z]{2,})\b', H_META + r'\1' + R, line)

        # epilog example lines: "  ppcm ./audio/    scan …"
        elif re.match(r'^  ppcm\b', line):
            m = re.match(r'^(  ppcm.*?)(\s{2,})(.+)$', line)
            if m:
                cmd, spc, desc = m.groups()
                cmd  = re.sub(r'\bppcm\b', H_CMD + 'ppcm' + R, cmd, count=1)
                line = cmd + spc + H_HELP + desc + R
            else:
                line = re.sub(r'\bppcm\b', H_CMD + 'ppcm' + R, line, count=1)

        out.append(line)

    return '\n'.join(out)


class ColorHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Argparse formatter that injects ANSI colours when the terminal supports them."""

    def format_help(self) -> str:
        text = super().format_help()
        if supports_color():
            text = _colorize(text)
        return text
