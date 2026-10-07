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


def _config(
    espacio_final: bool = True,
    selecciones: list[str] | None = None,
) -> ConfigSalida:
    """Crea un ConfigSalida para tests. selecciones por defecto: clipboard + primary."""
    return ConfigSalida(
        espacio_final=espacio_final,
        tecla_pegar="ctrl+v",
        selecciones=selecciones if selecciones is not None else ["clipboard", "primary"],
    )


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
        """Con dos selecciones: dos llamadas xclip seguidas de una xdotool."""
        runner = _RunnerCapture()
        cfg = _config(espacio_final=False)
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)

        salida.escribir("hola mundo")

        # clipboard + primary + xdotool = 3 llamadas
        self.assertEqual(len(runner.llamadas), 3)
        args_xclip0, datos_xclip0 = runner.llamadas[0]
        args_xclip1, datos_xclip1 = runner.llamadas[1]
        args_xdotool, datos_xdotool = runner.llamadas[2]

        self.assertEqual(args_xclip0[0], "xclip")
        self.assertEqual(datos_xclip0, "hola mundo".encode("utf-8"))
        self.assertEqual(args_xclip1[0], "xclip")
        self.assertEqual(datos_xclip1, "hola mundo".encode("utf-8"))
        self.assertEqual(args_xdotool[0], "xdotool")
        self.assertIsNone(datos_xdotool)

    def test_xclip_recibe_seleccion_clipboard(self) -> None:
        """La primera llamada a xclip usa '-selection' 'clipboard'."""
        runner = _RunnerCapture()
        cfg = _config(espacio_final=False)
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        salida.escribir("texto")

        args_xclip = runner.llamadas[0][0]
        self.assertIn("-selection", args_xclip)
        idx = args_xclip.index("-selection")
        self.assertEqual(args_xclip[idx + 1], "clipboard")

    def test_xdotool_usa_clearmodifiers(self) -> None:
        """xdotool debe incluir '--clearmodifiers' (última llamada del runner)."""
        runner = _RunnerCapture()
        cfg = _config(espacio_final=False)
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        salida.escribir("texto")

        # Con dos selecciones, xdotool es la llamada 2 (índice 2)
        args_xdotool = runner.llamadas[-1][0]
        self.assertIn("--clearmodifiers", args_xdotool)

    def test_xdotool_usa_tecla_de_config(self) -> None:
        """La tecla enviada a xdotool es la definida en la config (sin valor por defecto)."""
        runner = _RunnerCapture()
        cfg = _config(espacio_final=False)  # tecla_pegar="ctrl+v" en el helper de test
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        salida.escribir("texto")

        args_xdotool = runner.llamadas[-1][0]
        self.assertIn("ctrl+v", args_xdotool)

    def test_tecla_pegar_personalizable(self) -> None:
        """Si se inyecta tecla_pegar distinta, xdotool la usa."""
        runner = _RunnerCapture()
        cfg = _config(espacio_final=False)
        salida = SalidaX11(
            cfg,
            tecla_pegar="ctrl+shift+v",
            selecciones=cfg.selecciones,
            runner=runner,
        )
        salida.escribir("texto")

        args_xdotool = runner.llamadas[-1][0]
        self.assertIn("ctrl+shift+v", args_xdotool)
        self.assertNotIn("ctrl+v", args_xdotool)


# ---------------------------------------------------------------------------
# Tests de espacio final
# ---------------------------------------------------------------------------


class TestEspacioFinal(unittest.TestCase):
    def test_espacio_final_true_anade_espacio(self) -> None:
        runner = _RunnerCapture()
        cfg = _config(espacio_final=True)
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        salida.escribir("hola")

        _, datos = runner.llamadas[0]  # primera llamada: xclip clipboard
        self.assertEqual(datos, "hola ".encode("utf-8"))

    def test_espacio_final_false_no_anade_espacio(self) -> None:
        runner = _RunnerCapture()
        cfg = _config(espacio_final=False)
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        salida.escribir("hola")

        _, datos = runner.llamadas[0]
        self.assertEqual(datos, "hola".encode("utf-8"))

    def test_espacio_final_true_con_texto_ya_con_espacio(self) -> None:
        """Si el texto ya acaba en espacio se le añade otro; la config manda."""
        runner = _RunnerCapture()
        cfg = _config(espacio_final=True)
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        salida.escribir("hola ")

        _, datos = runner.llamadas[0]
        self.assertEqual(datos, "hola  ".encode("utf-8"))


# ---------------------------------------------------------------------------
# Tests de texto vacío
# ---------------------------------------------------------------------------


class TestTextoVacio(unittest.TestCase):
    def _assert_no_runner(self, texto: str) -> None:
        runner = _RunnerCapture()
        cfg = _config()
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
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
        cfg = _config(espacio_final=False)
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        texto = "Múñoz, ¿cómo estás?"
        salida.escribir(texto)

        # Verificar que todos los xclip reciben los mismos datos codificados
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

        cfg = _config()
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner_falla)
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
        """_runner_real transforma CalledProcessError en SalidaError.

        Con DEVNULL, exc.stderr siempre es None: el motivo solo incluye el
        código de retorno, sin fragmento de stderr.
        """
        from app.whisper_ptt.salida import _runner_real

        # stderr=None refleja lo que ocurre con DEVNULL (nunca hay captura).
        exc = subprocess.CalledProcessError(1, ["xdotool"], stderr=None)
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
# Tests unitarios — múltiples selecciones
# ---------------------------------------------------------------------------


class TestMultiplesSelecciones(unittest.TestCase):
    """Verifica el orden y contenido de las llamadas con una o varias selecciones."""

    def test_orden_selecciones_antes_de_tecla(self) -> None:
        """Todas las llamadas a xclip preceden a la llamada a xdotool."""
        runner = _RunnerCapture()
        cfg = _config(espacio_final=False, selecciones=["clipboard", "primary"])
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        salida.escribir("prueba")

        comandos = [llamada[0][0] for llamada in runner.llamadas]
        self.assertEqual(comandos, ["xclip", "xclip", "xdotool"])

    def test_una_sola_seleccion(self) -> None:
        """Con una sola selección hay una llamada xclip y una xdotool (total 2)."""
        runner = _RunnerCapture()
        cfg = _config(espacio_final=False, selecciones=["clipboard"])
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        salida.escribir("prueba")

        self.assertEqual(len(runner.llamadas), 2)
        self.assertEqual(runner.llamadas[0][0][0], "xclip")
        self.assertEqual(runner.llamadas[1][0][0], "xdotool")

    def test_dos_selecciones_clipboard_y_primary(self) -> None:
        """Con dos selecciones: xclip clipboard, xclip primary, luego xdotool."""
        runner = _RunnerCapture()
        cfg = _config(espacio_final=False, selecciones=["clipboard", "primary"])
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        salida.escribir("prueba")

        self.assertEqual(len(runner.llamadas), 3)

        args0 = runner.llamadas[0][0]
        self.assertIn("-selection", args0)
        self.assertEqual(args0[args0.index("-selection") + 1], "clipboard")

        args1 = runner.llamadas[1][0]
        self.assertIn("-selection", args1)
        self.assertEqual(args1[args1.index("-selection") + 1], "primary")

        self.assertEqual(runner.llamadas[2][0][0], "xdotool")

    def test_ambas_selecciones_reciben_mismo_contenido(self) -> None:
        """Todas las selecciones reciben los mismos bytes de contenido."""
        runner = _RunnerCapture()
        cfg = _config(espacio_final=False, selecciones=["clipboard", "primary"])
        salida = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        salida.escribir("mismo texto")

        datos0 = runner.llamadas[0][1]
        datos1 = runner.llamadas[1][1]
        self.assertEqual(datos0, datos1)
        self.assertEqual(datos0, "mismo texto".encode("utf-8"))


# ---------------------------------------------------------------------------
# Test de integración condicional
# ---------------------------------------------------------------------------

# Requiere DISPLAY y xclip, MÁS la variable de opt-in para tests de escritorio.
# Definir ESCRITURA_TESTS_INTEGRACION=1 para ejecutar tests que tocan el servidor X.
_XCLIP_DISPONIBLE = bool(
    shutil.which("xclip") and os.environ.get("DISPLAY") and os.environ.get("ESCRITURA_TESTS_INTEGRACION") == "1"
)


class TestIntegracionXclip(unittest.TestCase):
    @unittest.skipUnless(
        _XCLIP_DISPONIBLE,
        "xclip no disponible, DISPLAY no definido, o ESCRITURA_TESTS_INTEGRACION=1 no definido",
    )
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
        cfg = _config()
        instancia = SalidaX11(cfg, tecla_pegar=cfg.tecla_pegar, selecciones=cfg.selecciones, runner=runner)
        self.assertIsInstance(instancia, Salida)


if __name__ == "__main__":
    unittest.main()
