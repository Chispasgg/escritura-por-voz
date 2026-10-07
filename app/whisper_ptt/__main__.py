"""
Punto de entrada del daemon whisper-ptt.

Uso:
    python -m app.whisper_ptt [--config RUTA]

Construye todas las dependencias con inyección explícita en este orden:
  1. Configuración
  2. Transcriptor (carga el modelo; la operación más lenta)
  3. Grabador
  4. Salida
  5. Daemon (expone los callbacks al_pulsar / al_soltar)
  6. GestorHotkey (recibe los callbacks del daemon)
  7. daemon.arrancar() — bloquea en el bucle X11 hasta SIGTERM/SIGINT

Los errores de arranque terminan con código 1 y un mensaje claro a stderr.
Los errores en tiempo de ejecución (grabador, transcriptor, salida) son
registrados por el daemon y no terminan el proceso.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from app.whisper_ptt.config import (
    ConfigError,
    cargar_config,
    ruta_config_por_defecto,
)
from app.whisper_ptt.daemon import Daemon, DaemonYaEnMarcha
from app.whisper_ptt.grabador import GrabadorPipeWire
from app.whisper_ptt.hotkey import CombinacionInvalida, GestorHotkey, GrabFallido
from app.whisper_ptt.salida import SalidaX11
from app.whisper_ptt.transcriptor import TranscriptorError, TranscriptorWhisper


def _configurar_logging() -> None:
    """Logging a stderr con formato compacto. Nivel INFO por defecto."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    """
    Punto de entrada principal.

    Devuelve el código de salida (0 = ok, 1 = error de arranque).
    """
    _configurar_logging()
    log = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(
        prog="python -m app.whisper_ptt",
        description="Daemon de dictado push-to-talk con faster-whisper.",
    )
    parser.add_argument(
        "--config",
        metavar="RUTA",
        default=None,
        help=("Ruta al fichero config.ini. Por defecto: config/config.ini en la raíz del repositorio."),
    )
    args = parser.parse_args(argv)

    # ------------------------------------------------------------------
    # 1. Configuración
    # ------------------------------------------------------------------
    ruta_config = Path(args.config) if args.config else ruta_config_por_defecto()
    try:
        config = cargar_config(ruta_config)
    except ConfigError as exc:
        log.error("Configuración inválida: %s", exc)
        return 1

    # ------------------------------------------------------------------
    # 2. Transcriptor (carga el modelo; puede tardar ~3,5 s con caché)
    # ------------------------------------------------------------------
    log.info(
        "Cargando modelo '%s' en %s/%s…",
        config.whisper.modelo,
        config.whisper.dispositivo,
        config.whisper.tipo_computo,
    )
    t0 = time.monotonic()
    try:
        transcriptor = TranscriptorWhisper(config.whisper)
    except TranscriptorError as exc:
        log.error("Error al cargar el modelo: %s", exc)
        return 1
    log.info("Modelo cargado en %.1f s.", time.monotonic() - t0)

    # ------------------------------------------------------------------
    # 3. Grabador
    # ------------------------------------------------------------------
    # GrabadorPipeWire.__init__ solo almacena parámetros; no lanza.
    # El proceso pw-record se lanza más tarde, en iniciar().
    grabador = GrabadorPipeWire(
        frecuencia_muestreo=config.audio.frecuencia_muestreo,
        canales=config.audio.canales,
        duracion_minima_s=config.audio.duracion_minima_s,
    )

    # ------------------------------------------------------------------
    # 4. Salida
    # ------------------------------------------------------------------
    salida = SalidaX11(
        config.salida,
        tecla_pegar=config.salida.tecla_pegar,
        selecciones=config.salida.selecciones,
    )

    # ------------------------------------------------------------------
    # 5. Daemon — expone al_pulsar / al_soltar
    # ------------------------------------------------------------------
    daemon = Daemon(grabador, transcriptor, salida)

    # ------------------------------------------------------------------
    # 6. GestorHotkey — recibe los callbacks del daemon
    # ------------------------------------------------------------------
    gestor = GestorHotkey(
        config.tecla.combinacion,
        daemon.al_pulsar,
        daemon.al_soltar,
    )

    # ------------------------------------------------------------------
    # 7. Arrancar
    # ------------------------------------------------------------------
    ruta_pid = Path(config.general.fichero_pid)
    log.info(
        "Listo: mantén %s para dictar.",
        config.tecla.combinacion,
    )

    try:
        daemon.arrancar(gestor, ruta_pid=ruta_pid)
    except DaemonYaEnMarcha as exc:
        log.error("%s", exc)
        return 1
    except CombinacionInvalida as exc:
        log.error("Combinación de tecla inválida: %s", exc)
        return 1
    except GrabFallido as exc:
        log.error("No se pudo registrar el hotkey: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
