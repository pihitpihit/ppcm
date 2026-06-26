# ppcm

A command-line PCM audio file browser and player with a wizard-style bottom-pane
TUI. Pure Python standard library — no third-party dependencies. macOS only
(playback uses the built-in `afplay`).

## Install

Via Homebrew (tap):

```sh
brew install pihitpihit/tap/ppcm
```

Or from source:

```sh
pipx install .
```

## Usage

```sh
ppcm <directory>   # browse *.pcm files under a directory
ppcm <file.pcm>    # open a file directly (play screen)
ppcm --version
ppcm --help
```

## PCM format defaults

Duration/size calculations assume 16-bit signed, 22 050 Hz, mono. See
`CLAUDE.md` for the development guide.
