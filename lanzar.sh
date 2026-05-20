#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
COOKIE="/tmp/nerd-dictation.cookie"
MODEL_DIR="$SCRIPT_DIR/model"

# Guard: evitar segunda instancia
if [ -f "$COOKIE" ]; then
    PID=$(cat "$COOKIE" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        echo "El reconocimiento de voz ya está en marcha (PID $PID)."
        exit 0
    fi
fi

# Preparar entorno solo si hace falta
if [ ! -f "$VENV/bin/python" ]; then
    echo "Creando entorno virtual..."
    python3 -m venv "$VENV"
fi

if ! "$VENV/bin/python" -c "import vosk" 2>/dev/null; then
    echo "Instalando vosk..."
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet vosk
fi

echo "Iniciando reconocimiento de voz..."
"$VENV/bin/python" "$SCRIPT_DIR/app/nerd-dictation" begin \
    --vosk-model-dir="$MODEL_DIR" \
    --numbers-as-digits \
    --full-sentence \
    --continuous \
    &

echo "Listo. Usa parar.sh para detenerlo."
