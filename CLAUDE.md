# ppcm – development guide

## Overview

`ppcm` is a command-line PCM audio file browser and player with a wizard-style
bottom-pane TUI. It is invoked as:

```
ppcm <directory|file.pcm> [options]
```

It is packaged as an installable Python distribution (`pyproject.toml`,
src-layout) exposing a `ppcm` console-script entry point. It is distributed via
Homebrew tap and installed with `brew install pihitpihit/tap/ppcm`. No
third-party packages are required (standard library only).

For local development: `pip install -e .` (or `pipx install .`) inside a venv.

---

## File structure

```
ppcm/
  CLAUDE.md          ← this file
  README.md
  pyproject.toml     ← build config; [project.scripts] ppcm = "ppcm.cli:main"
  samples/           ← dev fixtures (*.pcm); excluded from the wheel
  src/ppcm/          ← the importable package
    __init__.py      ← __version__ (single source of truth for the version)
    cli.py           ← CLI entry point (thin: arg parsing + routing only)
    ansi.py          ← ANSI primitives (cuu, el, sgr) and colour palette
    pcm_utils.py     ← PCM metadata: pcm_duration, fmt_size, scan_pcm
    tui_list.py      ← ListTUI – bottom-pane file browser + read_key
    tui_play.py      ← PlayTUI – player screen
    help_fmt.py      ← ColorHelpFormatter – colourised argparse help (TTY-gated)
```

`cli.py` and all modules use package-relative imports (`from .ansi import …`).

New screens or features go in `src/ppcm/tui_<name>.py` and are imported by
`cli.py`.

---

## Versioning & release

The version lives only in `src/ppcm/__init__.py` (`__version__`); `pyproject.toml`
reads it dynamically via hatchling, and `cli.py` re-exports it for `--version`.

To release:
1. Bump `__version__` in `src/ppcm/__init__.py`, commit.
2. `git tag vX.Y.Z && git push --tags`, then `gh release create vX.Y.Z`.
3. Update `Formula/ppcm.rb` in the `pihitpihit/homebrew-tap` repo (local clone at
   `~/workspace/pihit/homebrew-tap`, sibling of this repo) with the new `url` and
   `sha256` (`curl -fsSL <tarball> | shasum -a 256`), commit and push; or run
   `brew bump-formula-pr`. Verify with `brew update && brew upgrade ppcm`.

Consumed by the `pihit_env` repo's `setup.sh` (`brew install pihitpihit/tap/ppcm`).

---

## PCM format defaults

All duration / size calculations assume:

| Parameter | Value |
|---|---|
| Bit depth | 16-bit signed |
| Sample rate | 22 050 Hz |
| Channels | 1 (mono) |

Constants live in `src/ppcm/pcm_utils.py` (`PCM_SAMPLE_RATE`, `PCM_CHANNELS`,
`PCM_BYTES_PER_SAMPLE`).

---

## TUI conventions

### Bottom-pane approach

The TUI occupies the last `TUI_H` lines of the terminal without clearing previous
output. The lifecycle is:

1. `sys.stdout.write('\n' * TUI_H + cuu(TUI_H))` — reserve space, rewind cursor
2. Draw `TUI_H` lines (each ending with `\r … \033[K \n`)
3. Redraw: `cuu(TUI_H)` then draw again
4. Exit cleanup: `cuu(TUI_H)` + clear each line + `cuu(TUI_H)`

Never use `curses.initscr()` or alternate-screen (`\033[?1049h`) — they would
hide previous terminal output, which contradicts the wizard-style requirement.

### Key input

Raw mode is entered with `tty.setraw()` / `termios.tcgetattr()` (stdlib only).
`read_key()` in `src/ppcm/tui_list.py` returns symbolic names: `'UP'`, `'DOWN'`,
`'ENTER'`, `'ESC'`, `'CTRL_C'`, or the literal character.

### Colour

All colour output must be gated on `supports_color()` from `ansi.py`, which
checks `sys.stdout.isatty()`. Never emit ANSI codes unconditionally.

The palette is defined in `src/ppcm/ansi.py`. Add new named constants there rather than
inlining `sgr(...)` calls in display logic.

---

## Screens

### 1 – PCM list (`tui_list.ListTUI`)

Shown when a directory is passed. Displays up to 15 files at a time with
scroll, shows relative path / size / duration per row.

Keys: `j` `k` `↑` `↓` — navigate · `↵` — select · `q` `ESC` — quit.

### 2 – PCM play (`tui_play.PlayTUI`)

Shown after a file is selected from the list, or when a file path is passed
directly. Plays via `afplay` (macOS built-in) with a temporary WAV wrapper
created from the raw PCM data using the stdlib `wave` module.

Layout (TUI_H = 8):
```
──────────────── PCM Player ────────────────
  filename.pcm
  2.345s · 103.2 KB

  [████████████████░░░░░░░░░░░░░░]  1.2 / 2.3s
  [ PLAYING ]
  [SPACE] pause/resume  [q/ESC] back
────────────────────────────────────────────
```

Keys: `SPACE` – pause/resume · `q` `ESC` – return to list.

Pause/resume uses `SIGSTOP`/`SIGCONT` on the `afplay` process.
Progress is computed from wall-clock time minus total paused duration.

---

## Adding a new screen

1. Create `src/ppcm/tui_<name>.py` with a class following the same lifecycle as
   `ListTUI` (reserve space → draw loop → cleanup → return result).
2. Import and invoke it from `cli.py` via `from .tui_<name> import …`.
3. Update this file.
