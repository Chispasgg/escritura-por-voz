"""
Módulo salida: pega texto en la ventana con foco mediante xclip + xdotool.

Diseño:
- Interfaz ``Salida`` (Protocol) con un único método ``escribir``.
- ``SalidaX11``: copia el texto al portapapeles con xclip (stdin, UTF-8) y
  simula la tecla de pegado con xdotool --clearmodifiers.
- La función de ejecución de comandos (``runner``) se inyecta en el
  constructor para poder testear sin X11 ni xclip reales.
- Texto vacío o solo espacios → no hace nada.
- Si falta un binario o el comando devuelve error, se lanza ``SalidaError``
  con información precisa (comando, código de retorno, stderr).
"""

from __future__ import annotations

import subprocess
from typing import Protocol, runtime_checkable

from app.whisper_ptt.config import ConfigSalida

# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------

# Un runner recibe (args, datos_stdin) y ejecuta el comando.
# Lanza SalidaError si falla o el binario no existe.
# Se inyecta en el constructor para poder sustituirlo por un doble en tests.
type Runner = callable  # ver firma completa más abajo; aquí solo para legibilidad

# ---------------------------------------------------------------------------
# Excepción propia
# ---------------------------------------------------------------------------


class SalidaError(Exception):
    """El comando externo xclip o xdotool falló o no está instalado."""

    def __init__(self, comando: str, motivo: str) -> None:
        self.comando = comando
        self.motivo = motivo
        super().__init__(f"{comando}: {motivo}")


# ---------------------------------------------------------------------------
# Interfaz
# ---------------------------------------------------------------------------


@runtime_checkable
class Salida(Protocol):
    """Interfaz mínima para pegar texto en la ventana con foco."""

    def escribir(self, texto: str) -> None:
        """Pega *texto* en la ventana activa. No hace nada si está vacío."""
        ...


# ---------------------------------------------------------------------------
# Runner por defecto (usa subprocess real)
# ---------------------------------------------------------------------------


def _runner_real(args: list[str], datos: bytes | None = None) -> None:
    """
    Ejecuta el comando externo descrito en *args*.

    Si *datos* no es None los escribe por stdin (útil para xclip).
    Lanza SalidaError si el binario no existe o el proceso termina con error.
    """
    nombre = args[0]
    try:
        subprocess.run(
            args,
            input=datos,
            # DEVNULL en lugar de capture_output: xclip se queda vivo en segundo
            # plano sirviendo la selección del portapapeles. Si capturásemos
            # stdout/stderr el SO mantendría los pipes abiertos y subprocess.run
            # bloquearía hasta que xclip muriese (varios segundos). Con DEVNULL
            # los pipes no existen y el retorno es inmediato.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=5,
        )
    except FileNotFoundError:
        raise SalidaError(nombre, f"binario '{nombre}' no encontrado en PATH") from None
    except subprocess.TimeoutExpired:
        raise SalidaError(nombre, "tiempo de espera agotado (5 s)") from None
    except subprocess.CalledProcessError as exc:
        # Con DEVNULL, exc.stderr siempre es None: solo incluimos el código de retorno.
        raise SalidaError(nombre, f"código de retorno {exc.returncode}") from None


# ---------------------------------------------------------------------------
# Implementación X11
# ---------------------------------------------------------------------------


class SalidaX11:
    """
    Pega texto en la ventana activa mediante xclip + xdotool.

    Pasos:
      1. Para cada selección configurada, copia el texto con
         ``xclip -selection <selección>`` (texto por stdin, UTF-8).
         Copiar a clipboard y primary permite usar shift+Insert tanto en
         WezTerm/xterm (pega PRIMARY) como en GTK/Firefox (pega CLIPBOARD).
      2. Simula la tecla de pegado con
         ``xdotool key --clearmodifiers <tecla_pegar>``.
         ``--clearmodifiers`` limpia el estado de modificadores activos
         (p. ej. Control o Escape remanentes del hotkey).

    Args:
        config_salida: Sección ``[salida]`` de la configuración. Determina
            si se añade un espacio al final.
        tecla_pegar: Combinación que xdotool debe simular para pegar.
            Requerido; se pasa desde ``config.salida.tecla_pegar``.
        selecciones: Lista de selecciones X11 a las que copiar el texto.
            Requerido; se pasa desde ``config.salida.selecciones``.
        runner: Función que ejecuta un comando externo. Se inyecta para
            facilitar los tests sin X11.
    """

    def __init__(
        self,
        config_salida: ConfigSalida,
        *,
        tecla_pegar: str,
        selecciones: list[str],
        runner=_runner_real,
    ) -> None:
        self._espacio_final: bool = config_salida.espacio_final
        self._tecla_pegar: str = tecla_pegar
        self._selecciones: list[str] = selecciones
        self._runner = runner

    def escribir(self, texto: str) -> None:
        """
        Pega *texto* en la ventana activa.

        No hace nada si *texto* está vacío o es solo espacios (evita
        pegar nada o perder el portapapeles del usuario innecesariamente).
        """
        if not texto.strip():
            return

        contenido = texto
        if self._espacio_final:
            contenido = contenido + " "

        datos = contenido.encode("utf-8")

        # 1. Copiar a cada selección X11.
        #    xclip se queda vivo en segundo plano sirviendo la selección; el
        #    runner usa DEVNULL para no bloquear esperando que cierre sus pipes.
        for seleccion in self._selecciones:
            self._runner(["xclip", "-selection", seleccion], datos)

        # 2. Simular la tecla de pegado.
        self._runner(
            ["xdotool", "key", "--clearmodifiers", self._tecla_pegar],
            None,
        )
