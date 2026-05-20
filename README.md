# Nerd Dictation

*Offline Speech to Text for Desktop Linux.*

Utility that provides speech-to-text for Linux without being tied to a desktop environment, using [VOSK-API](https://github.com/alphacep/vosk-api). Single-file Python script with minimal dependencies.

## Usage

Bind `begin`/`end`/`cancel` to shortcut keys.

```sh
nerd-dictation begin
nerd-dictation end
```

## Install

```sh
pip3 install vosk
git clone https://github.com/ideasman42/nerd-dictation.git
cd nerd-dictation
wget https://alphacephei.com/kaldi/models/vosk-model-small-en-us-0.15.zip
unzip vosk-model-small-en-us-0.15.zip
mv vosk-model-small-en-us-0.15 model
```

To test:

```sh
./nerd-dictation begin --vosk-model-dir=./model &
# Start speaking.
./nerd-dictation end
```

Move model to default path to avoid passing `--vosk-model-dir` every time:

```sh
mkdir -p ~/.config/nerd-dictation
mv ./model ~/.config/nerd-dictation
```

## Dependencies

- Python 3.6+
- `vosk` (pip)
- Audio input: `parec` (PulseAudio, default), `sox`, or `pw-cat` (PipeWire)
- Input simulation: `xdotool` (X11, default), `ydotool`, `dotool`, `wtype` (Wayland)

## Configuration

Place at `~/.config/nerd-dictation/nerd-dictation.py`:

```python
def nerd_dictation_process(text):
    return text.upper()
```

See `examples/` for more complete configurations (word replacement, begin/end commands, grammar files).

## Features

- **Numbers as digits** — `--numbers-as-digits`: "three hundred" → "300"
- **Timeout** — `--timeout SECONDS`: end automatically when no speech detected
- **Output modes** — keystroke simulation (default) or `--output=STDOUT`
- **Suspend/Resume** — keep process in memory between sessions to avoid reload delay
- **Grammar files** — restrict recognized phrases via `--vosk-grammar-file` for better accuracy

## Commands

| Command | Description |
|---------|-------------|
| `begin` | Start dictation |
| `end` | Stop and type recognized text |
| `cancel` | Stop without typing |
| `suspend` | Pause (keeps model loaded) |
| `resume` | Resume after suspend |

Run `nerd-dictation begin --help` for all options.

## Paths

| Path | Purpose |
|------|---------|
| `~/.config/nerd-dictation/nerd-dictation.py` | User configuration |
| `~/.config/nerd-dictation/model` | Language model (default location) |
