"""
Transcriptor de audio a texto mediante faster-whisper.

Diseño:
- Protocolo TranscriptorProtocol: contrato mínimo transcribir(audio) -> str,
  inyectable en tests y en el daemon.
- TranscriptorWhisper: implementación real que carga el modelo una sola vez
  en el constructor (ver nota de diseño abajo) y llama a transcribe() de
  faster-whisper con los parámetros de ConfigWhisper.
- La construcción del WhisperModel se delega en una fábrica inyectada
  (FabricaModelo) para poder testear sin GPU ni descarga del modelo.
- Excepción propia TranscriptorError con código para distinguir fallo de
  carga y fallo de transcripción.
- Audio vacío → "" sin llamar al modelo.
- Segmentos concatenados con un espacio simple; se eliminan espacios sobrantes.

Nota sobre cuándo cargar el modelo:
  El modelo se carga en __init__, no de forma perezosa. Razón: el daemon lo
  construye una sola vez al arrancar y luego espera; si la carga falla
  (CUDA no disponible, modelo inexistente), es mejor fallar pronto, con un
  mensaje claro, antes de que el usuario empiece a dictar, en lugar de
  fallar en la primera transcripción cuando no hay oportunidad de corregir
  la configuración. La alternativa (carga explícita con un método cargar())
  añadiría estado mutable y complejidad sin beneficio real en este caso de uso.
"""

from __future__ import annotations

from typing import Callable, Protocol

import numpy as np

from app.whisper_ptt.config import ConfigWhisper

# ---------------------------------------------------------------------------
# Tipos para inyección de dependencias
# ---------------------------------------------------------------------------

# La fábrica recibe (nombre_modelo, dispositivo, tipo_computo) y devuelve
# una instancia del modelo. En producción construye un WhisperModel real;
# en tests devuelve un doble.
FabricaModelo = Callable[[str, str, str], object]


def _fabrica_real(nombre: str, dispositivo: str, tipo_computo: str) -> object:
    """Fábrica por defecto: construye un WhisperModel de faster-whisper real."""
    # La importación se hace aquí (no en el módulo) para que el módulo sea
    # importable incluso si faster-whisper no está instalado (p. ej. en entornos
    # de CI que no tienen CUDA). La clase se importará solo cuando se use la
    # fábrica real.
    from faster_whisper import WhisperModel  # type: ignore[import]

    return WhisperModel(nombre, device=dispositivo, compute_type=tipo_computo)


# ---------------------------------------------------------------------------
# Excepción propia
# ---------------------------------------------------------------------------


class TranscriptorError(Exception):
    """Error del transcriptor: fallo al cargar el modelo o al transcribir."""

    def __init__(self, codigo: str, detalle: str) -> None:
        self.codigo = codigo
        self.detalle = detalle
        super().__init__(f"[{codigo}] {detalle}")


# Códigos de error públicos
COD_CARGA_MODELO = "CARGA_MODELO"  # El modelo no se pudo cargar
COD_TRANSCRIPCION = "TRANSCRIPCION"  # La transcripción lanzó una excepción


# ---------------------------------------------------------------------------
# Protocolo (interfaz)
# ---------------------------------------------------------------------------


class TranscriptorProtocol(Protocol):
    """Interfaz del transcriptor: convierte audio numpy en texto."""

    def transcribir(self, audio: np.ndarray) -> str:
        """
        Transcribe *audio* a texto.

        Args:
            audio: ndarray float32 mono [-1, 1] a 16 kHz.
                   Un array vacío (len == 0) devuelve "" sin llamar al modelo.

        Returns:
            Texto transcrito, o "" si el audio estaba vacío o no se detectó habla.
        """
        ...


# ---------------------------------------------------------------------------
# Implementación faster-whisper
# ---------------------------------------------------------------------------


class TranscriptorWhisper:
    """
    Transcribe audio a texto usando faster-whisper.

    El modelo se carga una sola vez en el constructor y se reutiliza en
    todas las llamadas a transcribir(). Ver nota de diseño en la cabecera
    del módulo sobre la elección de carga en __init__ frente a carga perezosa.

    Args:
        config: Sección [whisper] de la configuración.
        fabrica: Callable(nombre, dispositivo, tipo_computo) -> modelo.
                 Por defecto usa faster_whisper.WhisperModel. Se inyecta
                 en tests para evitar descarga y GPU.

    Raises:
        TranscriptorError(COD_CARGA_MODELO): si el modelo no se puede cargar
            (CUDA/cuDNN no disponible, nombre de modelo incorrecto, etc.).
    """

    def __init__(
        self,
        config: ConfigWhisper,
        fabrica: FabricaModelo = _fabrica_real,
    ) -> None:
        self._config = config

        # prompt_inicial vacío → None (faster-whisper lo trata distinto a "")
        self._prompt: str | None = config.prompt_inicial.strip() or None

        try:
            self._modelo = fabrica(
                config.modelo,
                config.dispositivo,
                config.tipo_computo,
            )
        except Exception as exc:
            raise TranscriptorError(
                COD_CARGA_MODELO,
                f"No se pudo cargar el modelo '{config.modelo}' "
                f"en dispositivo='{config.dispositivo}', "
                f"tipo_computo='{config.tipo_computo}': {exc}. "
                "Si CUDA falla, prueba dispositivo=cpu y tipo_computo=int8.",
            ) from exc

    def transcribir(self, audio: np.ndarray) -> str:
        """
        Transcribe *audio* a texto.

        Devuelve "" si el array está vacío (grabación demasiado corta descartada
        por el grabador) sin invocar al modelo.

        Raises:
            TranscriptorError(COD_TRANSCRIPCION): si faster-whisper lanza
                una excepción inesperada durante la transcripción.
        """
        # Array vacío: grabación descartada por el grabador (duración mínima)
        if len(audio) == 0:
            return ""

        try:
            segmentos, _info = self._modelo.transcribe(  # type: ignore[union-attr]
                audio,
                language=self._config.idioma,
                vad_filter=self._config.filtro_vad,
                beam_size=self._config.tamano_haz,
                initial_prompt=self._prompt,
            )
            # Unir los textos de los segmentos eliminando espacios sobrantes.
            # faster-whisper devuelve un generador; lo recorremos una sola vez.
            texto = " ".join(seg.text.strip() for seg in segmentos if seg.text.strip())
        except Exception as exc:
            raise TranscriptorError(
                COD_TRANSCRIPCION,
                f"Error durante la transcripción: {exc}",
            ) from exc

        return texto
