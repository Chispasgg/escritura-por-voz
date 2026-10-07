#!/bin/bash
# Para el motor de dictado que esté en marcha según config/config.ini.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

if [ ! -f "$VENV/bin/python" ]; then
    echo "El entorno virtual no existe; no hay motor en marcha."
    exit 0
fi

# Leer motor y ruta del fichero PID a través del cargador Python validado.
# Salida: línea 1 = motor, línea 2 = fichero_pid.
_config=$(
    PYTHONPATH="$SCRIPT_DIR" "$VENV/bin/python" -c "
from app.whisper_ptt.config import cargar_config
c = cargar_config()
print(c.general.motor)
print(c.general.fichero_pid)
"
)
{ read -r MOTOR; read -r FICHERO_PID; } <<< "$_config"

if [ "$MOTOR" = "whisper" ]; then
    if [ ! -f "$FICHERO_PID" ]; then
        echo "El daemon whisper-ptt no está en marcha."
        exit 0
    fi

    _pid=$(cat "$FICHERO_PID" 2>/dev/null || true)
    if [ -z "$_pid" ] || ! kill -0 "$_pid" 2>/dev/null; then
        echo "El daemon whisper-ptt no está en marcha (PID obsoleto; limpiando)."
        rm -f "$FICHERO_PID"
        exit 0
    fi

    echo "Deteniendo daemon whisper-ptt (PID $_pid)..."
    # El proceso podría haber terminado entre la comprobación kill -0 y este punto.
    # En ese caso kill -TERM falla: lo tratamos como «ya había terminado».
    if ! kill -TERM "$_pid" 2>/dev/null; then
        echo "El daemon ya había terminado."
        rm -f "$FICHERO_PID"
        exit 0
    fi

    # El daemon atrapa SIGTERM y espera al hilo trabajador (hasta 5 s internos).
    # Aquí esperamos hasta 10 s antes de forzar con SIGKILL.
    _timeout=10
    _elapsed=0
    while kill -0 "$_pid" 2>/dev/null; do
        if [ "$_elapsed" -ge "$_timeout" ]; then
            echo "Aviso: el daemon no terminó en ${_timeout} s. Forzando terminación."
            kill -KILL "$_pid" 2>/dev/null || true
            break
        fi
        sleep 1
        _elapsed=$((_elapsed + 1))
    done
    echo "Daemon detenido."

elif [ "$MOTOR" = "vosk" ]; then
    COOKIE="/tmp/nerd-dictation.cookie"

    if [ ! -f "$COOKIE" ]; then
        echo "El reconocimiento de voz no está en marcha."
        exit 0
    fi

    echo "Parando el reconocimiento de voz (VOSK)..."
    "$SCRIPT_DIR/app/nerd-dictation" end
fi
