#!/bin/bash
# Arranca el motor de dictado configurado en config/config.ini.
# El motor activo se lee a través del cargador Python validado para no duplicar
# la lógica de parseo del INI con grep/sed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

# ── 1. Entorno virtual ────────────────────────────────────────────────────────
if [ ! -f "$VENV/bin/python" ]; then
    echo "Creando entorno virtual..."
    python3 -m venv "$VENV"
fi

# ── 2. Dependencias ──────────────────────────────────────────────────────────
# faster_whisper, Xlib (python-xlib) y vosk son las tres dependencias del proyecto.
# Si falta cualquiera, se instala todo requirements.txt de una sola vez.
if ! "$VENV/bin/python" -c "import faster_whisper, Xlib, vosk" 2>/dev/null; then
    echo "Instalando dependencias..."
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi

# ── 3. Leer configuración ────────────────────────────────────────────────────
# Se llama a cargar_config() para que la validación sea la misma que usa el daemon,
# evitando duplicar el parseo del INI. Salida: motor, fichero_pid, directorio_modelo.
_config=$(
    PYTHONPATH="$SCRIPT_DIR" "$VENV/bin/python" -c "
from app.whisper_ptt.config import cargar_config
c = cargar_config()
print(c.general.motor)
print(c.general.fichero_pid)
print(c.vosk.directorio_modelo)
"
)
{ read -r MOTOR; read -r FICHERO_PID; read -r DIR_MODELO_VOSK; } <<< "$_config"

# ── 4. Arrancar según motor ──────────────────────────────────────────────────
if [ "$MOTOR" = "whisper" ]; then
    # Comprobación de instancia: el daemon crea su propio PID con O_EXCL, pero
    # verificar aquí permite dar un mensaje claro antes de intentar arrancar.
    if [ -f "$FICHERO_PID" ]; then
        _pid=$(cat "$FICHERO_PID" 2>/dev/null || true)
        if [ -n "$_pid" ] && kill -0 "$_pid" 2>/dev/null; then
            echo "El daemon whisper-ptt ya está en marcha (PID $_pid)."
            exit 0
        fi
    fi

    # Log en el mismo directorio que el fichero PID (XDG_RUNTIME_DIR o /tmp),
    # con extensión .log. Así ambos ficheros de runtime quedan en el mismo lugar.
    LOG="${FICHERO_PID%.pid}.log"

    echo "Iniciando daemon whisper-ptt (cargando modelo, puede tardar unos segundos la primera vez)..."
    echo "Log en: $LOG"
    PYTHONPATH="$SCRIPT_DIR" "$VENV/bin/python" -m app.whisper_ptt \
        >>"$LOG" 2>&1 &
    echo "Listo. Usa parar.sh para detenerlo."

elif [ "$MOTOR" = "vosk" ]; then
    # Comportamiento idéntico al original: cookie de nerd-dictation en /tmp.
    COOKIE="/tmp/nerd-dictation.cookie"

    if [ -f "$COOKIE" ]; then
        _pid=$(cat "$COOKIE" 2>/dev/null || true)
        if [ -n "$_pid" ] && kill -0 "$_pid" 2>/dev/null; then
            echo "El reconocimiento de voz ya está en marcha (PID $_pid)."
            exit 0
        fi
    fi

    echo "Iniciando reconocimiento de voz (VOSK)..."
    "$VENV/bin/python" "$SCRIPT_DIR/app/nerd-dictation" begin \
        --vosk-model-dir="$SCRIPT_DIR/$DIR_MODELO_VOSK" \
        --config="$SCRIPT_DIR/app/config/nerd-dictation.py" \
        --simulate-input-tool=XCLIP \
        --input=PW-CAT \
        --sample-rate=16000 \
        --numbers-as-digits \
        --continuous \
        &
    echo "Listo. Usa parar.sh para detenerlo."
fi
