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
    tecla_pegar: str  # combinación que xdotool envía para pegar
    selecciones: list[str]  # selecciones X11 a las que se copia: p. ej. ["clipboard", "primary"]


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

_SELECCIONES_VALIDAS = frozenset({"clipboard", "primary"})

_BOOL_TRUE = frozenset({"true", "yes", "on", "1"})
_BOOL_FALSE = frozenset({"false", "no", "off", "0"})


def _obtener_requerido(parser: configparser.ConfigParser, seccion: str, campo: str) -> str:
    """Lee un campo obligatorio de una sección; lanza ConfigError si está ausente o vacío.

    Separar esta detección de los validadores numéricos y booleanos evita mensajes
    confusos como «debe ser un entero, se recibió ''» cuando el campo simplemente
    no está en el fichero.
    """
    valor = parser.get(seccion, campo, fallback=None)
    if valor is None or not valor.strip():
        raise ConfigError(seccion, campo, "campo obligatorio ausente o vacío")
    return valor.strip()


def _selecciones(seccion: str, campo: str, valor: str) -> list[str]:
    """Parsea y valida la lista de selecciones X11 del portapapeles.

    Valores permitidos: 'clipboard', 'primary'. Al menos uno. Sin duplicados.
    """
    items = [item.strip().lower() for item in valor.split(",") if item.strip()]
    if not items:
        raise ConfigError(seccion, campo, "debe contener al menos un valor ('clipboard', 'primary')")
    invalidos = [i for i in items if i not in _SELECCIONES_VALIDAS]
    if invalidos:
        raise ConfigError(
            seccion,
            campo,
            f"valores no permitidos: {invalidos}; se aceptan {sorted(_SELECCIONES_VALIDAS)}",
        )
    # Detectar duplicados preservando el orden de aparición
    vistos: set[str] = set()
    duplicados: list[str] = []
    for i in items:
        if i in vistos:
            duplicados.append(i)
        else:
            vistos.add(i)
    if duplicados:
        raise ConfigError(seccion, campo, f"valores duplicados: {duplicados}")
    return items


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
        raise ConfigError(seccion, campo, f"debe ser un entero, se recibió '{valor}'") from None
    if n <= 0:
        raise ConfigError(seccion, campo, f"debe ser positivo, se recibió {n}")
    return n


def _float_positivo(seccion: str, campo: str, valor: str) -> float:
    try:
        n = float(valor)
    except ValueError:
        raise ConfigError(seccion, campo, f"debe ser un número, se recibió '{valor}'") from None
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


def _expandir_pid(raw: str) -> str:
    """Sustituye $XDG_RUNTIME_DIR por su valor del entorno; usa /tmp como ruta de respaldo.

    En una sesión gráfica con systemd $XDG_RUNTIME_DIR siempre existe (típicamente
    /run/user/<uid>). /tmp es solo el fallback para entornos sin systemd o sin sesión
    de usuario (p. ej. scripts de arranque, SSH sin ForwardX11).

    Se usa una sustitución explícita en lugar de os.path.expandvars para controlar
    exactamente qué variable se expande y aplicar el fallback de forma predecible.
    """
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
        ConfigError: Si el fichero no existe, falta una sección obligatoria,
                     falta un campo obligatorio o cualquier valor es inválido.
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
        motor=_motor(_obtener_requerido(parser, sec, "motor")),
        fichero_pid=_expandir_pid(_obtener_requerido(parser, sec, "fichero_pid")),
    )

    # [tecla]
    sec = "tecla"
    tecla = ConfigTecla(
        combinacion=_obtener_requerido(parser, sec, "combinacion"),
    )

    # [audio]
    sec = "audio"
    audio = ConfigAudio(
        frecuencia_muestreo=_entero_positivo(
            sec, "frecuencia_muestreo", _obtener_requerido(parser, sec, "frecuencia_muestreo")
        ),
        canales=_entero_positivo(sec, "canales", _obtener_requerido(parser, sec, "canales")),
        duracion_minima_s=_float_positivo(
            sec, "duracion_minima_s", _obtener_requerido(parser, sec, "duracion_minima_s")
        ),
    )

    # [whisper]
    sec = "whisper"
    whisper = ConfigWhisper(
        modelo=_obtener_requerido(parser, sec, "modelo"),
        dispositivo=_obtener_requerido(parser, sec, "dispositivo"),
        tipo_computo=_obtener_requerido(parser, sec, "tipo_computo"),
        idioma=_obtener_requerido(parser, sec, "idioma"),
        filtro_vad=_booleano(sec, "filtro_vad", _obtener_requerido(parser, sec, "filtro_vad")),
        tamano_haz=_entero_positivo(sec, "tamano_haz", _obtener_requerido(parser, sec, "tamano_haz")),
        # prompt_inicial es opcional: se permite vacío
        prompt_inicial=parser.get(sec, "prompt_inicial", fallback=""),
    )

    # [salida]
    sec = "salida"
    salida = ConfigSalida(
        espacio_final=_booleano(sec, "espacio_final", _obtener_requerido(parser, sec, "espacio_final")),
        tecla_pegar=_obtener_requerido(parser, sec, "tecla_pegar"),
        selecciones=_selecciones(sec, "selecciones", _obtener_requerido(parser, sec, "selecciones")),
    )

    # [vosk]
    sec = "vosk"
    vosk = ConfigVosk(
        directorio_modelo=_obtener_requerido(parser, sec, "directorio_modelo"),
    )

    return Config(
        general=general,
        tecla=tecla,
        audio=audio,
        whisper=whisper,
        salida=salida,
        vosk=vosk,
    )
