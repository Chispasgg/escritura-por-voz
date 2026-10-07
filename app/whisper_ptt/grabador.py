"""
Grabador de audio vía PipeWire (pw-record).

Diseño:
- Protocolo GrabadorProtocol: contrato mínimo iniciar/detener, inyectable en tests.
- GrabadorPipeWire: implementación real que lanza pw-record sin shell=True,
  lee su stdout en un hilo para evitar que el pipe se llene y bloquee.
- Excepción propia GrabadorError con código para distinguir los casos de error.
- detener() termina el proceso siempre (terminate → wait con timeout → kill),
  incluso ante excepciones; nunca deja procesos zombis.
- Audio devuelto como numpy.ndarray float32 mono normalizado a [-1, 1],
  a la frecuencia configurada en [audio] (16 kHz), listo para faster-whisper.
- Si la grabación dura menos que duracion_minima_s se devuelve un array vacío
  para que el daemon la descarte sin transcribir.
"""

from __future__ import annotations

import io
import subprocess
import threading
import time
from typing import Callable, Protocol

import numpy as np

# ---------------------------------------------------------------------------
# Excepción propia
# ---------------------------------------------------------------------------


class GrabadorError(Exception):
    """Error del grabador: proceso no disponible, estado incorrecto o fallo de E/S."""

    def __init__(self, codigo: str, detalle: str) -> None:
        self.codigo = codigo
        self.detalle = detalle
        super().__init__(f"[{codigo}] {detalle}")


# Códigos de error públicos
COD_NO_INICIADO = "NO_INICIADO"  # detener() sin iniciar() previo
COD_YA_INICIADO = "YA_INICIADO"  # iniciar() cuando ya hay una grabación activa
COD_PROCESO_FALLO = "PROCESO_FALLO"  # pw-record no existe o terminó con error


# ---------------------------------------------------------------------------
# Protocolo (interfaz)
# ---------------------------------------------------------------------------


class GrabadorProtocol(Protocol):
    """Interfaz del grabador: arrancar captura y obtener el audio al parar."""

    def iniciar(self) -> None:
        """Arranca la captura de audio. Lanza GrabadorError si ya está activo."""
        ...

    def detener(self) -> np.ndarray:
        """
        Detiene la captura y devuelve el audio.

        Returns:
            ndarray float32 mono [-1, 1] a la frecuencia configurada,
            o un array vacío si la duración fue menor que duracion_minima_s.

        Lanza GrabadorError si no había grabación activa.
        """
        ...


# ---------------------------------------------------------------------------
# Tipo para inyectar el lanzador del proceso (facilita tests)
# ---------------------------------------------------------------------------

# El lanzador recibe la lista de argumentos y devuelve un Popen.
LanzadorProceso = Callable[[list[str]], subprocess.Popen]


def _lanzador_real(args: list[str]) -> subprocess.Popen:
    """Lanzador por defecto: usa subprocess sin shell=True."""
    try:
        return subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except FileNotFoundError:
        raise GrabadorError(
            COD_PROCESO_FALLO,
            f"No se encontró el ejecutable '{args[0]}'. ¿Está instalado pw-record?",
        ) from None


# ---------------------------------------------------------------------------
# Implementación PipeWire
# ---------------------------------------------------------------------------

# Bytes por muestra para el formato s16 (PCM signed 16-bit little-endian)
_BYTES_POR_MUESTRA = 2
_RANGO_S16 = 32768.0  # 2^15 — divisor para normalizar a [-1, 1]


class GrabadorPipeWire:
    """
    Graba audio del micrófono a memoria usando pw-record.

    pw-record escribe PCM s16 little-endian mono a stdout; lo leemos en un
    hilo de fondo para que el buffer del pipe nunca se llene.

    Args:
        frecuencia_muestreo: Hz (debe coincidir con [audio].frecuencia_muestreo).
        canales:             1 para mono (faster-whisper exige mono).
        duracion_minima_s:   Grabaciones más cortas dan un array vacío.
        ejecutable:          Ruta a pw-record (por defecto "pw-record" en PATH).
        lanzador:            Inyectable para tests; por defecto usa subprocess.
    """

    def __init__(
        self,
        frecuencia_muestreo: int,
        canales: int,
        duracion_minima_s: float,
        ejecutable: str = "pw-record",
        lanzador: LanzadorProceso = _lanzador_real,
    ) -> None:
        self._frecuencia_muestreo = frecuencia_muestreo
        self._canales = canales
        self._duracion_minima_s = duracion_minima_s
        self._ejecutable = ejecutable
        self._lanzador = lanzador

        self._proceso: subprocess.Popen | None = None
        self._hilo_lector: threading.Thread | None = None
        self._buffer: io.BytesIO | None = None
        self._inicio_s: float | None = None

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def iniciar(self) -> None:
        """
        Arranca pw-record y el hilo lector de stdout.

        Raises:
            GrabadorError(COD_YA_INICIADO): si ya hay una grabación activa.
            GrabadorError(COD_PROCESO_FALLO): si pw-record no se puede lanzar.
        """
        if self._proceso is not None:
            raise GrabadorError(
                COD_YA_INICIADO,
                "Ya hay una grabación activa. Llama a detener() primero.",
            )

        # Construir argumentos sin shell=True; destino '-' = stdout
        args = [
            self._ejecutable,
            "--rate",
            str(self._frecuencia_muestreo),
            "--channels",
            str(self._canales),
            "--format",
            "s16",
            "-",  # destino stdout (equivale a --output=-)
        ]

        self._buffer = io.BytesIO()
        self._inicio_s = time.monotonic()
        # _lanzador puede lanzar GrabadorError si el ejecutable no existe
        self._proceso = self._lanzador(args)

        # Hilo de lectura: drena stdout para que el pipe nunca se llene.
        # Usamos daemon=True para que no bloquee la salida del proceso principal.
        self._hilo_lector = threading.Thread(
            target=_leer_stdout,
            args=(self._proceso, self._buffer),
            daemon=True,
        )
        self._hilo_lector.start()

    def detener(self) -> np.ndarray:
        """
        Detiene la grabación y devuelve el audio como ndarray float32 mono.

        Returns:
            ndarray float32 normalizado a [-1, 1], o array vacío si la
            grabación fue más corta que duracion_minima_s.

        Raises:
            GrabadorError(COD_NO_INICIADO): si no había grabación activa.
        """
        if self._proceso is None:
            raise GrabadorError(
                COD_NO_INICIADO,
                "No hay grabación activa. Llama a iniciar() primero.",
            )

        duracion = time.monotonic() - self._inicio_s  # type: ignore[operator]

        # Terminar el proceso de forma segura siempre, incluso ante excepciones
        proceso = self._proceso
        hilo = self._hilo_lector
        buffer = self._buffer

        self._proceso = None
        self._hilo_lector = None
        self._buffer = None
        self._inicio_s = None

        try:
            _terminar_proceso(proceso)
        finally:
            # Esperar a que el hilo de lectura termine de vaciar el pipe
            if hilo is not None:
                hilo.join(timeout=2.0)
            # Cerrar explícitamente stdout para evitar file descriptor leaks
            if proceso.stdout is not None:
                try:
                    proceso.stdout.close()
                except OSError:
                    pass

        # Duración mínima: si la grabación es muy corta, descartarla
        if duracion < self._duracion_minima_s:
            return np.array([], dtype=np.float32)

        audio_bytes = buffer.getvalue()  # type: ignore[union-attr]
        return _bytes_s16_a_float32(audio_bytes, self._canales)


# ---------------------------------------------------------------------------
# Funciones auxiliares privadas
# ---------------------------------------------------------------------------


def _terminar_proceso(proceso: subprocess.Popen) -> None:
    """
    Termina el proceso de forma limpia: terminate → wait(2s) → kill.
    No lanza excepción aunque el proceso ya haya terminado.
    El llamador debe asegurarse de que no se llama dos veces sobre el mismo proceso.
    """
    try:
        proceso.terminate()
    except OSError:
        # El proceso ya terminó: no es un error
        pass

    try:
        proceso.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        # Expiró el plazo: forzar con kill
        try:
            proceso.kill()
        except OSError:
            pass
        try:
            proceso.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass  # En el peor caso el OS limpiará el zombi


def _leer_stdout(proceso: subprocess.Popen, buffer: io.BytesIO) -> None:
    """
    Lee stdout del proceso en bloques y los acumula en buffer.
    Diseñado para ejecutarse en un hilo daemon.
    """
    assert proceso.stdout is not None
    try:
        while True:
            bloque = proceso.stdout.read(4096)
            if not bloque:
                break
            buffer.write(bloque)
    except OSError:
        # El pipe se cerró (proceso terminado): normal al llamar detener()
        pass


def _bytes_s16_a_float32(datos: bytes, canales: int) -> np.ndarray:
    """
    Convierte bytes PCM s16le a ndarray float32 mono normalizado a [-1, 1].

    Si hay más de un canal se promedia (downmix) para obtener mono.
    Devuelve array vacío si no hay datos suficientes.
    """
    if len(datos) < _BYTES_POR_MUESTRA:
        return np.array([], dtype=np.float32)

    # Número de muestras por canal
    n_muestras_totales = len(datos) // _BYTES_POR_MUESTRA
    n_frames = n_muestras_totales // canales
    n_muestras_utiles = n_frames * canales

    if n_muestras_utiles == 0:
        return np.array([], dtype=np.float32)

    # Decodificar PCM s16 little-endian a int16, luego convertir a float32
    muestras = np.frombuffer(datos[: n_muestras_utiles * _BYTES_POR_MUESTRA], dtype=np.int16).astype(np.float32)

    muestras /= _RANGO_S16  # normalizar a [-1, 1]

    if canales > 1:
        # Reshape a (n_frames, canales) y promediar para obtener mono
        muestras = muestras.reshape(n_frames, canales).mean(axis=1)

    return muestras
