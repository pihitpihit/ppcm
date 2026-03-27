#!/usr/bin/env python3
"""ppcm - PCM audio file player with TUI interface.

Usage:
    ppcm <directory>   browse *.pcm files under directory
    ppcm <file.pcm>    open file directly (play screen)
    ppcm --version
    ppcm --help
"""

import sys
import os
import argparse

VERSION = "0.1.0"

# local modules
sys.path.insert(0, os.path.dirname(__file__))
from ansi       import supports_color
from pcm_utils  import scan_pcm
from tui_list   import ListTUI
from help_fmt   import ColorHelpFormatter


def main():
    parser = argparse.ArgumentParser(
        prog='ppcm',
        description='PCM audio file player',
        formatter_class=ColorHelpFormatter,
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
