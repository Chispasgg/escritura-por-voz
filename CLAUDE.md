# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`nerd-dictation` is a single-file offline speech-to-text utility for desktop Linux, built on [VOSK-API](https://alphacephei.com/vosk/). The entire implementation lives in the `nerd-dictation` file (no `.py` extension).

## Setup

```sh
pip install vosk
# Download a model and place it at ~/.config/nerd-dictation/model
# Or use --vosk-model-dir=./model with a local path
```

Local convenience scripts (use a venv):
```sh
./lanzar.sh   # start dictation in background (sets up venv, installs vosk)
./parar.sh    # stop dictation
```

## Usage

```sh
./nerd-dictation begin --vosk-model-dir=./model &
./nerd-dictation end      # end and type recognized text
./nerd-dictation cancel   # end without typing
./nerd-dictation suspend  # pause (SIGUSR1)
./nerd-dictation resume   # resume (SIGCONT)
```

## Code quality

```sh
# Format
black nerd-dictation

# Type check
mypy --strict nerd-dictation

# Lint
pylint nerd-dictation --disable=C0103,C0111,C0301,C0302,C0415,E0401,E0611,I1101,R0801,R0902,R0903,R0912,R0913,R0914,R0915,R1705,W0212,W0703
```

`black` is configured in `pyproject.toml`: line length 119, target Python 3.10.

## Running tests

Tests are standalone scripts, not pytest:

```sh
python3 tests/from_words_to_digits.py
```

Watch mode (Linux):
```sh
bash -c 'while true; do inotifywait -e close_write nerd-dictation tests/from_words_to_digits.py; tests/from_words_to_digits.py; done'
```

## Architecture

**Single-file design is intentional.** Do not split into modules. Only stdlib + `vosk` are used.

The file is organized in sections (top to bottom):

1. **General utilities** — file ops, subprocess helpers, `execfile()`
2. **Simulate input backends** — one function per tool: `xdotool`, `ydotool`, `dotool`/`dotoolc`, `wtype`, stdout
3. **User config** — loads `~/.config/nerd-dictation/nerd-dictation.py` via `execfile()`; exposes `nerd_dictation_process(text) -> str`
4. **Number parsing** — `from_words_to_digits` class converts spoken numbers to digits (e.g. "three hundred" → "300")
5. **Text processing** — `process_text()` applies number conversion and capitalization; `process_text_with_user_config()` runs user hook
6. **Recording + VOSK pipeline** — `recording_proc_with_non_blocking_stdout()` spawns audio recorder; `text_from_vosk_pipe()` is the main loop
7. **Command implementations** — `main_begin()`, `main_end()`, `main_cancel()`, `main_suspend()`
8. **Argparse** — one `argparse_create_*` function per subcommand

### Cookie mechanism

Inter-process signaling uses a temp file (`/tmp/nerd-dictation.cookie`):
- `begin` writes its PID and sets mtime=0
- `end` touches the file (changes mtime) → `begin` detects and exits
- `cancel` removes the file → `begin` detects and exits with code -1
- `suspend`/`resume` send SIGUSR1/SIGCONT to the PID stored in the cookie

### Stdout vs stderr

All diagnostic output goes to **stderr**. Stdout is reserved exclusively for dictated text (when `--output=STDOUT`).

### Recording startup order

Recording starts before VOSK loads to avoid missing the beginning of speech. `vosk` import is deliberately deferred inside `text_from_vosk_pipe()`.

### Supported audio inputs

`PAREC` (PulseAudio, default), `SOX`, `PW-CAT` (PipeWire)

### Supported input simulation tools

`XDOTOOL` (X11, default), `YDOTOOL`, `DOTOOL`, `DOTOOLC`, `WTYPE` (Wayland), `STDOUT`

## User configuration

Place at `~/.config/nerd-dictation/nerd-dictation.py`:
```python
def nerd_dictation_process(text):
    return text.upper()  # example
```

The function is called after built-in processing. Return `""` to suppress output. See `examples/` for more patterns.
