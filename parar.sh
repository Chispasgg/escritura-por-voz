#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COOKIE="/tmp/nerd-dictation.cookie"

if [ ! -f "$COOKIE" ]; then
    echo "El reconocimiento de voz no está en marcha."
    exit 0
fi

echo "Parando el reconocimiento de voz..."
"$SCRIPT_DIR/app/nerd-dictation" end
