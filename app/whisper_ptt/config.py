"""
Carga y validación de la configuración desde config/config.ini.

Diseño:
- Sin estado global: cada llamada a cargar_config() devuelve un árbol de
  dataclasses inmutables independiente.
- interpolation=None en configparser para no interpretar '$' ni '%' en los valores.
- ConfigError lleva sección, campo y motivo para que el llamador pueda mostrar
  un mensaje preciso sin parsear el string de la excepción.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Excepción propia
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Error de configuración: campo inválido, valor fuera de rango o fichero ausente."""

    def __init__(self, seccion: str, campo: str, motivo: str) -> None:
        self.seccion = seccion
        self.campo = campo
        self.motivo = motivo
        super().__init__(f"[{seccion}] {campo}: {motivo}")


# ---------------------------------------------------------------------------
# Dataclasses de configuración (inmutables)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigGeneral:
    motor: str  # "whisper" | "vosk"
    fichero_pid: str  # ruta al fichero PID, ya expandida


@dataclass(frozen=True)
class ConfigTecla:
    combinacion: str  # formato X11: "Modificador+keysym"


@dataclass(frozen=True)
class ConfigAudio:
    frecuencia_muestreo: int  # Hz; positivo
    canales: int  # positivo
    duracion_minima_s: float  # segundos; grabaciones más cortas se descartan


@dataclass(frozen=True)
class ConfigWhisper:
    modelo: str
    dispositivo: str
    tipo_computo: str
    idioma: str
    filtro_vad: bool
    tamano_haz: int  # positivo
    prompt_inicial: str  # puede estar vacío


@dataclass(frozen=True)
class ConfigSalida:
    espacio_final: bool


@dataclass(frozen=True)
class ConfigVosk:
    directorio_modelo: str  # ruta relativa a la raíz del repositorio


@dataclass(frozen=True)
class Config:
    """Árbol completo de configuración. Inmutable una vez construido."""

    general: ConfigGeneral
    tecla: ConfigTecla
    audio: ConfigAudio
    whisper: ConfigWhisper
    salida: ConfigSalida
    vosk: ConfigVosk


# ---------------------------------------------------------------------------
# Validadores privados
# ---------------------------------------------------------------------------

_MOTORES_VALIDOS = frozenset({"whisper", "vosk"})

_BOOL_TRUE = frozenset({"true", "yes", "on", "1"})
_BOOL_FALSE = frozenset({"false", "no", "off", "0"})


def _motor(valor: str) -> str:
    normalizado = valor.strip().lower()
    if normalizado not in _MOTORES_VALIDOS:
        raise ConfigError(
            "general",
            "motor",
            f"debe ser uno de {sorted(_MOTORES_VALIDOS)}, se recibió '{valor}'",
        )
    return normalizado


def _entero_positivo(seccion: str, campo: str, valor: str) -> int:
    try:
        n = int(valor)
    except ValueError:
        raise ConfigError(
            seccion, campo, f"debe ser un entero, se recibió '{valor}'"
        ) from None
    if n <= 0:
        raise ConfigError(seccion, campo, f"debe ser positivo, se recibió {n}")
    return n


def _float_positivo(seccion: str, campo: str, valor: str) -> float:
    try:
        n = float(valor)
    except ValueError:
        raise ConfigError(
            seccion, campo, f"debe ser un número, se recibió '{valor}'"
        ) from None
    if n <= 0:
        raise ConfigError(seccion, campo, f"debe ser positivo, se recibió {n}")
    return n


def _booleano(seccion: str, campo: str, valor: str) -> bool:
    normalizado = valor.strip().lower()
    if normalizado in _BOOL_TRUE:
        return True
    if normalizado in _BOOL_FALSE:
        return False
    raise ConfigError(seccion, campo, f"debe ser true/false, se recibió '{valor}'")


def _no_vacio(seccion: str, campo: str, valor: str) -> str:
    """Valida que el valor no sea vacío ni solo espacios."""
    if not valor.strip():
        raise ConfigError(seccion, campo, "no puede estar vacío")
    return valor.strip()


def _expandir_pid(raw: str) -> str:
    """Sustituye $XDG_RUNTIME_DIR por su valor del entorno; usa /tmp si no está definido."""
    # Usamos una sustitución explícita en lugar de os.path.expandvars para
    # controlar exactamente qué variable se expande y aplicar el fallback.
    directorio = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    return raw.replace("$XDG_RUNTIME_DIR", directorio)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def ruta_config_por_defecto() -> Path:
    """Devuelve la ruta canónica a config/config.ini en la raíz del repositorio."""
    # Este módulo está en app/whisper_ptt/; la raíz del repo está dos niveles arriba.
    raiz = Path(__file__).parent.parent.parent
    return raiz / "config" / "config.ini"


def cargar_config(ruta: Path | str | None = None) -> Config:
    """
    Carga y valida la configuración desde un fichero INI.

    Args:
        ruta: Ruta al fichero. Si es None se usa ruta_config_por_defecto().

    Returns:
        Árbol de dataclasses inmutables con la configuración validada.

    Raises:
        ConfigError: Si el fichero no existe, falta una sección obligatoria
                     o cualquier valor es inválido.
    """
    if ruta is None:
        ruta = ruta_config_por_defecto()
    ruta = Path(ruta)

    if not ruta.exists():
        raise ConfigError("config", "ruta", f"fichero no encontrado: {ruta}")

    # interpolation=None evita que configparser trate '$' o '%' como metasintaxis
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ruta, encoding="utf-8")

    secciones_requeridas = ("general", "tecla", "audio", "whisper", "salida", "vosk")
    for sec in secciones_requeridas:
        if not parser.has_section(sec):
            raise ConfigError(
                sec,
                "sección",
                "sección obligatoria ausente en el fichero de configuración",
            )

    # [general]
    sec = "general"
    general = ConfigGeneral(
        motor=_motor(parser.get(sec, "motor", fallback="")),
        fichero_pid=_expandir_pid(
            _no_vacio(sec, "fichero_pid", parser.get(sec, "fichero_pid", fallback=""))
        ),
    )

    # [tecla]
    sec = "tecla"
    tecla = ConfigTecla(
        combinacion=_no_vacio(
            sec, "combinacion", parser.get(sec, "combinacion", fallback="")
        ),
    )

    # [audio]
    sec = "audio"
    audio = ConfigAudio(
        frecuencia_muestreo=_entero_positivo(
            sec,
            "frecuencia_muestreo",
            parser.get(sec, "frecuencia_muestreo", fallback=""),
        ),
        canales=_entero_positivo(
            sec, "canales", parser.get(sec, "canales", fallback="")
        ),
        duracion_minima_s=_float_positivo(
            sec, "duracion_minima_s", parser.get(sec, "duracion_minima_s", fallback="")
        ),
    )

    # [whisper]
    sec = "whisper"
    whisper = ConfigWhisper(
        modelo=_no_vacio(sec, "modelo", parser.get(sec, "modelo", fallback="")),
        dispositivo=_no_vacio(
            sec, "dispositivo", parser.get(sec, "dispositivo", fallback="")
        ),
        tipo_computo=_no_vacio(
            sec, "tipo_computo", parser.get(sec, "tipo_computo", fallback="")
        ),
        idioma=_no_vacio(sec, "idioma", parser.get(sec, "idioma", fallback="")),
        filtro_vad=_booleano(
            sec, "filtro_vad", parser.get(sec, "filtro_vad", fallback="")
        ),
        tamano_haz=_entero_positivo(
            sec, "tamano_haz", parser.get(sec, "tamano_haz", fallback="")
        ),
        prompt_inicial=parser.get(sec, "prompt_inicial", fallback=""),
    )

    # [salida]
    sec = "salida"
    salida = ConfigSalida(
        espacio_final=_booleano(
            sec, "espacio_final", parser.get(sec, "espacio_final", fallback="")
        ),
    )

    # [vosk]
    sec = "vosk"
    vosk = ConfigVosk(
        directorio_modelo=_no_vacio(
            sec, "directorio_modelo", parser.get(sec, "directorio_modelo", fallback="")
        ),
    )

    return Config(
        general=general,
        tecla=tecla,
        audio=audio,
        whisper=whisper,
        salida=salida,
        vosk=vosk,
    )
