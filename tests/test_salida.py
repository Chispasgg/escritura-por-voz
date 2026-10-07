"""
Tests del módulo app.whisper_ptt.salida.

Sin X11, sin xclip, sin xdotool: todos los comandos externos se sustituyen
por dobles (funciones lambda o clases de captura).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest
from unittest.mock import patch

from app.whisper_ptt.config import ConfigSalida
from app.whisper_ptt.salida import Salida, SalidaError, SalidaX11

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(espacio_final: bool = True) -> ConfigSalida:
    return ConfigSalida(espacio_final=espacio_final, tecla_pegar="ctrl+v")


class _RunnerCapture:
    """Doble que captura todas las llamadas al runner."""

    def __init__(self) -> None:
        self.llamadas: list[tuple[list[str], bytes | None]] = []

    def __call__(self, args: list[str], datos: bytes | None = None) -> None:
        self.llamadas.append((args, datos))


# ---------------------------------------------------------------------------
# Tests de comportamiento básico
# ---------------------------------------------------------------------------


class TestSalidaX11Escribe(unittest.TestCase):
    def test_texto_normal_pasa_por_xclip_y_xdotool(self) -> None:
        """El runner debe recibir exactamente dos llamadas: xclip y xdotool."""
        runner = _RunnerCapture()
        salida = SalidaX11(_config(espacio_final=False), runner=runner)

        salida.escribir("hola mundo")

        self.assertEqual(len(runner.llamadas), 2)
        args_xclip, datos_xclip = runner.llamadas[0]
        args_xdotool, datos_xdotool = runner.llamadas[1]

        self.assertEqual(args_xclip[0], "xclip")
        self.assertEqual(datos_xclip, "hola mundo".encode("utf-8"))
        self.assertEqual(args_xdotool[0], "xdotool")
        self.assertIsNone(datos_xdotool)

    def test_xclip_recibe_seleccion_clipboard(self) -> None:
        """xclip debe recibir '-selection' 'clipboard' como argumentos."""
        runner = _RunnerCapture()
        salida = SalidaX11(_config(espacio_final=False), runner=runner)
        salida.escribir("texto")

        args_xclip = runner.llamadas[0][0]
        self.assertIn("-selection", args_xclip)
        idx = args_xclip.index("-selection")
        self.assertEqual(args_xclip[idx + 1], "clipboard")

    def test_xdotool_usa_clearmodifiers(self) -> None:
        """xdotool debe incluir '--clearmodifiers'."""
        runner = _RunnerCapture()
        salida = SalidaX11(_config(espacio_final=False), runner=runner)
        salida.escribir("texto")

        args_xdotool = runner.llamadas[1][0]
        self.assertIn("--clearmodifiers", args_xdotool)

    def test_xdotool_tecla_por_defecto_ctrl_v(self) -> None:
        """La tecla por defecto enviada a xdotool es 'ctrl+v'."""
        runner = _RunnerCapture()
        salida = SalidaX11(_config(espacio_final=False), runner=runner)
        salida.escribir("texto")

        args_xdotool = runner.llamadas[1][0]
        self.assertIn("ctrl+v", args_xdotool)

    def test_tecla_pegar_personalizable(self) -> None:
        """Si se inyecta tecla_pegar distinta, xdotool la usa."""
        runner = _RunnerCapture()
        salida = SalidaX11(
            _config(espacio_final=False),
            tecla_pegar="ctrl+shift+v",
            runner=runner,
        )
        salida.escribir("texto")

        args_xdotool = runner.llamadas[1][0]
        self.assertIn("ctrl+shift+v", args_xdotool)
        self.assertNotIn("ctrl+v", args_xdotool)


# ---------------------------------------------------------------------------
# Tests de espacio final
# ---------------------------------------------------------------------------


class TestEspacioFinal(unittest.TestCase):
    def test_espacio_final_true_anade_espacio(self) -> None:
        runner = _RunnerCapture()
        salida = SalidaX11(_config(espacio_final=True), runner=runner)
        salida.escribir("hola")

        _, datos = runner.llamadas[0]
        self.assertEqual(datos, "hola ".encode("utf-8"))

    def test_espacio_final_false_no_anade_espacio(self) -> None:
        runner = _RunnerCapture()
        salida = SalidaX11(_config(espacio_final=False), runner=runner)
        salida.escribir("hola")

        _, datos = runner.llamadas[0]
        self.assertEqual(datos, "hola".encode("utf-8"))

    def test_espacio_final_true_con_texto_ya_con_espacio(self) -> None:
        """Si el texto ya acaba en espacio se le añade otro; la config manda."""
        runner = _RunnerCapture()
        salida = SalidaX11(_config(espacio_final=True), runner=runner)
        salida.escribir("hola ")

        _, datos = runner.llamadas[0]
        self.assertEqual(datos, "hola  ".encode("utf-8"))


# ---------------------------------------------------------------------------
# Tests de texto vacío
# ---------------------------------------------------------------------------


class TestTextoVacio(unittest.TestCase):
    def _assert_no_runner(self, texto: str) -> None:
        runner = _RunnerCapture()
        salida = SalidaX11(_config(), runner=runner)
        salida.escribir(texto)
        self.assertEqual(runner.llamadas, [], f"se esperaban 0 llamadas para '{texto!r}'")

    def test_texto_vacio(self) -> None:
        self._assert_no_runner("")

    def test_texto_solo_espacios(self) -> None:
        self._assert_no_runner("   ")

    def test_texto_solo_tabs_y_saltos(self) -> None:
        self._assert_no_runner("\t\n  \n")


# ---------------------------------------------------------------------------
# Tests de encoding (tildes y ñ)
# ---------------------------------------------------------------------------


class TestEncoding(unittest.TestCase):
    def test_tildes_y_enie_codifican_utf8(self) -> None:
        runner = _RunnerCapture()
        salida = SalidaX11(_config(espacio_final=False), runner=runner)
        texto = "Múñoz, ¿cómo estás?"
        salida.escribir(texto)

        _, datos = runner.llamadas[0]
        self.assertEqual(datos, texto.encode("utf-8"))
        # Verificar que los bytes realmente difieren del ASCII (hay bytes >127)
        self.assertTrue(any(b > 127 for b in datos))


# ---------------------------------------------------------------------------
# Tests de errores del runner
# ---------------------------------------------------------------------------


class TestSalidaError(unittest.TestCase):
    def test_runner_que_lanza_salida_error_se_propaga(self) -> None:
        """Si el runner lanza SalidaError, escribir() lo deja burbujear."""

        def runner_falla(args, datos=None):
            raise SalidaError(args[0], "binario no encontrado")

        salida = SalidaX11(_config(), runner=runner_falla)
        with self.assertRaises(SalidaError):
            salida.escribir("hola")

    def test_salida_error_tiene_atributos_comando_y_motivo(self) -> None:
        err = SalidaError("xclip", "binario 'xclip' no encontrado en PATH")
        self.assertEqual(err.comando, "xclip")
        self.assertIn("xclip", err.motivo)

    def test_runner_real_lanza_salida_error_si_binario_ausente(self) -> None:
        """
        Verifica que _runner_real transforma FileNotFoundError en SalidaError.

        Se parchea subprocess.run para simular binario ausente sin ejecutar nada.
        """
        from app.whisper_ptt.salida import _runner_real

        with patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(SalidaError) as ctx:
                _runner_real(["xclip", "-selection", "clipboard"], b"test")
            self.assertEqual(ctx.exception.comando, "xclip")
            self.assertIn("no encontrado", ctx.exception.motivo)

    def test_runner_real_lanza_salida_error_si_proceso_falla(self) -> None:
        """_runner_real transforma CalledProcessError en SalidaError."""
        from app.whisper_ptt.salida import _runner_real

        exc = subprocess.CalledProcessError(1, ["xdotool"], stderr=b"error X11")
        with patch("subprocess.run", side_effect=exc):
            with self.assertRaises(SalidaError) as ctx:
                _runner_real(["xdotool", "key", "ctrl+v"], None)
            self.assertEqual(ctx.exception.comando, "xdotool")
            self.assertIn("1", ctx.exception.motivo)  # código de retorno

    def test_runner_real_lanza_salida_error_en_timeout(self) -> None:
        from app.whisper_ptt.salida import _runner_real

        exc = subprocess.TimeoutExpired(["xclip"], timeout=5)
        with patch("subprocess.run", side_effect=exc):
            with self.assertRaises(SalidaError) as ctx:
                _runner_real(["xclip", "-selection", "clipboard"], b"x")
            self.assertIn("tiempo", ctx.exception.motivo)


# ---------------------------------------------------------------------------
# Test de integración condicional
# ---------------------------------------------------------------------------

_XCLIP_DISPONIBLE = bool(shutil.which("xclip") and os.environ.get("DISPLAY"))


class TestIntegracionXclip(unittest.TestCase):
    @unittest.skipUnless(_XCLIP_DISPONIBLE, "xclip no disponible o DISPLAY no definido")
    def test_runner_real_xclip_retorna_rapido(self) -> None:
        """
        xclip no debe bloquear: con DEVNULL en stdout/stderr el runner
        debe retornar en menos de 1 s aunque xclip siga vivo sirviendo la
        selección en segundo plano.
        """
        from app.whisper_ptt.salida import _runner_real

        inicio = time.monotonic()
        _runner_real(["xclip", "-selection", "clipboard"], b"test integracion")
        duracion = time.monotonic() - inicio
        self.assertLess(duracion, 1.0, f"xclip tardó {duracion:.3f} s (esperado < 1 s)")


# ---------------------------------------------------------------------------
# Tests de interfaz (Protocol)
# ---------------------------------------------------------------------------


class TestProtocolo(unittest.TestCase):
    def test_salida_x11_implementa_protocolo(self) -> None:
        """SalidaX11 debe satisfacer el Protocol Salida en runtime."""
        runner = _RunnerCapture()
        instancia = SalidaX11(_config(), runner=runner)
        self.assertIsInstance(instancia, Salida)


if __name__ == "__main__":
    unittest.main()
