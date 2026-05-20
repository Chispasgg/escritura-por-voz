#!/bin/bash
echo "lanzando el reconocimiento de voz en segundo plano"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install vosk
# ./nerd-dictation begin --vosk-model-dir=./model --simulate-input-tool=WTYPE &
./nerd-dictation begin --vosk-model-dir=./model &

# ./nerd-dictation begin --vosk-model-dir=./model --input=PW-CAT --output=STDOUT --timeout=2