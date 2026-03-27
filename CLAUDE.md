# ppcm – development guide

## Overview

`ppcm` is a command-line PCM audio file browser and player with a wizard-style
bottom-pane TUI. It is invoked as:

```
ppcm <directory|file.pcm> [options]
```

The alias `alias ppcm='python3 ~/pihit_env/tools/anna/ppcm/ppcm.py'` is
registered in `~/.bashrc`. No third-party packages are required.

---

## File structure

| File | Responsibility |
|---|---|
| `ppcm.py` | CLI entry point: argument parsing, top-level routing |
| `ansi.py` | ANSI escape primitives (`cuu`, `el`, `sgr`) and the colour palette |
| `pcm_utils.py` | PCM metadata: `pcm_duration`, `fmt_size`, `scan_pcm` |
| `tui_list.py` | `ListTUI` – bottom-pane file browser + `read_key` input reader |
| `help_fmt.py` | `ColorHelpFormatter` – colourised argparse help (TTY-gated) |

New screens or features should be placed in their own module and imported by
`ppcm.py`. Keep `ppcm.py` thin (routing only).

---

## PCM format defaults

All duration / size calculations assume:

| Parameter | Value |
|---|---|
| Bit depth | 16-bit signed |
| Sample rate | 22 050 Hz |
| Channels | 1 (mono) |

Constants live in `pcm_utils.py` (`PCM_SAMPLE_RATE`, `PCM_CHANNELS`,
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
`read_key()` in `tui_list.py` returns symbolic names: `'UP'`, `'DOWN'`,
`'ENTER'`, `'ESC'`, `'CTRL_C'`, or the literal character.

### Colour

All colour output must be gated on `supports_color()` from `ansi.py`, which
checks `sys.stdout.isatty()`. Never emit ANSI codes unconditionally.

The palette is defined in `ansi.py`. Add new named constants there rather than
inlining `sgr(...)` calls in display logic.

---

## Screens

### 1 – PCM list (`tui_list.ListTUI`)

Shown when a directory is passed. Displays up to 15 files at a time with
scroll, shows relative path / size / duration per row.

Keys: `j` `k` `↑` `↓` — navigate · `↵` — select · `q` `ESC` — quit.

### 2 – PCM play (not yet implemented)

Shown after a file is selected from the list, or when a file path is passed
directly. Spec TBD. Current placeholder: prints the selected path and exits.

---

## Adding a new screen

1. Create `tui_<name>.py` with a class following the same lifecycle as
   `ListTUI` (reserve space → draw loop → cleanup → return result).
2. Import and invoke it from `ppcm.py`.
3. Update this file.
