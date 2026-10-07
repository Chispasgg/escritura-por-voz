"""
Tests del módulo app.whisper_ptt.config.

Se usan ficheros INI temporales; no se accede a hardware, GPU, X11 ni al
config real del sistema. Cada test es independiente y limpia sus temporales.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.whisper_ptt.config import ConfigError, cargar_config, ruta_config_por_defecto

# ---------------------------------------------------------------------------
# INI base válido — todos los tests lo usan como punto de partida
# ---------------------------------------------------------------------------

_INI_VALIDO = """\
[general]
motor = whisper
fichero_pid = /tmp/evp.pid

[tecla]
combinacion = Control+Escape

[audio]
frecuencia_muestreo = 16000
canales = 1
duracion_minima_s = 0.3

[whisper]
modelo = large-v3-turbo
dispositivo = cuda
tipo_computo = float16
idioma = es
filtro_vad = true
tamano_haz = 5
prompt_inicial =

[salida]
espacio_final = true
tecla_pegar = ctrl+v
selecciones = clipboard,primary

[vosk]
directorio_modelo = model
"""

# INI sin la sección [whisper], para probar secciones ausentes
_INI_SIN_WHISPER = """\
[general]
motor = whisper
fichero_pid = /tmp/evp.pid

[tecla]
combinacion = Control+Escape

[audio]
frecuencia_muestreo = 16000
canales = 1
duracion_minima_s = 0.3

[salida]
espacio_final = true
tecla_pegar = ctrl+v

[vosk]
directorio_modelo = model
"""

# INI con [whisper] completa pero sin el campo "modelo", para probar campo ausente
_INI_WHISPER_SIN_MODELO = """\
[general]
motor = whisper
fichero_pid = /tmp/evp.pid

[tecla]
combinacion = Control+Escape

[audio]
frecuencia_muestreo = 16000
canales = 1
duracion_minima_s = 0.3

[whisper]
dispositivo = cuda
tipo_computo = float16
idioma = es
filtro_vad = true
tamano_haz = 5
prompt_inicial =

[salida]
espacio_final = true
tecla_pegar = ctrl+v

[vosk]
directorio_modelo = model
"""


def _escribir_ini(contenido: str) -> Path:
    """Escribe el contenido en un fichero temporal y devuelve su ruta."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False, encoding="utf-8")
    f.write(contenido)
    f.close()
    return Path(f.name)


class TestCargaValida(unittest.TestCase):
    """Verifica que un INI correcto produce el árbol de config esperado."""

    def setUp(self):
        self.ruta = _escribir_ini(_INI_VALIDO)

    def tearDown(self):
        self.ruta.unlink(missing_ok=True)

    def test_valores_generales(self):
        cfg = cargar_config(self.ruta)
        self.assertEqual(cfg.general.motor, "whisper")
        self.assertEqual(cfg.general.fichero_pid, "/tmp/evp.pid")

    def test_valores_audio(self):
        cfg = cargar_config(self.ruta)
        self.assertEqual(cfg.audio.frecuencia_muestreo, 16000)
        self.assertEqual(cfg.audio.canales, 1)
        self.assertAlmostEqual(cfg.audio.duracion_minima_s, 0.3)

    def test_valores_whisper(self):
        cfg = cargar_config(self.ruta)
        self.assertEqual(cfg.whisper.modelo, "large-v3-turbo")
        self.assertEqual(cfg.whisper.dispositivo, "cuda")
        self.assertTrue(cfg.whisper.filtro_vad)
        self.assertEqual(cfg.whisper.tamano_haz, 5)
        self.assertEqual(cfg.whisper.prompt_inicial, "")

    def test_salida_y_vosk(self):
        cfg = cargar_config(self.ruta)
        self.assertTrue(cfg.salida.espacio_final)
        self.assertEqual(cfg.salida.tecla_pegar, "ctrl+v")
        self.assertEqual(cfg.salida.selecciones, ["clipboard", "primary"])
        self.assertEqual(cfg.vosk.directorio_modelo, "model")

    def test_motor_vosk_valido(self):
        ruta = _escribir_ini(_INI_VALIDO.replace("motor = whisper", "motor = vosk"))
        try:
            cfg = cargar_config(ruta)
            self.assertEqual(cfg.general.motor, "vosk")
        finally:
            ruta.unlink(missing_ok=True)

    def test_config_inmutable(self):
        cfg = cargar_config(self.ruta)
        with self.assertRaises((AttributeError, TypeError)):
            cfg.general.motor = "vosk"  # type: ignore[misc]


class TestValidaciones(unittest.TestCase):
    """Cada test comprueba que un valor inválido lanza ConfigError con el campo correcto."""

    def _cargar_con(self, buscar: str, reemplazar: str) -> None:
        """Carga un INI derivado del válido con una línea modificada."""
        contenido = _INI_VALIDO.replace(buscar, reemplazar, 1)
        ruta = _escribir_ini(contenido)
        try:
            cargar_config(ruta)
        finally:
            ruta.unlink(missing_ok=True)

    def test_motor_invalido(self):
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("motor = whisper", "motor = openai")
        err = ctx.exception
        self.assertEqual(err.seccion, "general")
        self.assertEqual(err.campo, "motor")

    def test_frecuencia_muestreo_negativa(self):
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("frecuencia_muestreo = 16000", "frecuencia_muestreo = -1")
        self.assertEqual(ctx.exception.campo, "frecuencia_muestreo")

    def test_frecuencia_muestreo_no_entero(self):
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("frecuencia_muestreo = 16000", "frecuencia_muestreo = abc")
        self.assertEqual(ctx.exception.campo, "frecuencia_muestreo")

    def test_canales_cero(self):
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("canales = 1", "canales = 0")
        self.assertEqual(ctx.exception.campo, "canales")

    def test_duracion_minima_negativa(self):
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("duracion_minima_s = 0.3", "duracion_minima_s = -0.5")
        self.assertEqual(ctx.exception.campo, "duracion_minima_s")

    def test_duracion_minima_no_numero(self):
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("duracion_minima_s = 0.3", "duracion_minima_s = dos")
        self.assertEqual(ctx.exception.campo, "duracion_minima_s")

    def test_filtro_vad_invalido(self):
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("filtro_vad = true", "filtro_vad = quizas")
        self.assertEqual(ctx.exception.campo, "filtro_vad")

    def test_tamano_haz_cero(self):
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("tamano_haz = 5", "tamano_haz = 0")
        self.assertEqual(ctx.exception.campo, "tamano_haz")

    def test_combinacion_vacia(self):
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("combinacion = Control+Escape", "combinacion =")
        self.assertEqual(ctx.exception.campo, "combinacion")

    def test_espacio_final_invalido(self):
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("espacio_final = true", "espacio_final = quizas")
        self.assertEqual(ctx.exception.campo, "espacio_final")

    def test_seccion_ausente(self):
        ruta = _escribir_ini(_INI_SIN_WHISPER)
        try:
            with self.assertRaises(ConfigError) as ctx:
                cargar_config(ruta)
            self.assertEqual(ctx.exception.seccion, "whisper")
        finally:
            ruta.unlink(missing_ok=True)

    def test_campo_ausente_en_seccion(self):
        """[whisper] existe pero falta 'modelo': el error debe nombrar sección y campo."""
        ruta = _escribir_ini(_INI_WHISPER_SIN_MODELO)
        try:
            with self.assertRaises(ConfigError) as ctx:
                cargar_config(ruta)
            err = ctx.exception
            self.assertEqual(err.seccion, "whisper")
            self.assertEqual(err.campo, "modelo")
            self.assertIn("ausente", err.motivo)
        finally:
            ruta.unlink(missing_ok=True)

    def test_tecla_pegar_vacia(self):
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("tecla_pegar = ctrl+v", "tecla_pegar =")
        self.assertEqual(ctx.exception.campo, "tecla_pegar")

    def test_selecciones_valor_invalido(self):
        """Valores no permitidos en 'selecciones' → ConfigError con campo correcto."""
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("selecciones = clipboard,primary", "selecciones = clipboard,xclipboard")
        err = ctx.exception
        self.assertEqual(err.campo, "selecciones")
        self.assertIn("no permitidos", err.motivo)

    def test_selecciones_vacia(self):
        """Campo 'selecciones' vacío → ConfigError."""
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("selecciones = clipboard,primary", "selecciones =")
        self.assertEqual(ctx.exception.campo, "selecciones")

    def test_selecciones_duplicados(self):
        """Valores duplicados en 'selecciones' → ConfigError."""
        with self.assertRaises(ConfigError) as ctx:
            self._cargar_con("selecciones = clipboard,primary", "selecciones = clipboard,clipboard")
        err = ctx.exception
        self.assertEqual(err.campo, "selecciones")
        self.assertIn("duplicados", err.motivo)

    def test_selecciones_una_sola_valida(self):
        """Una sola selección válida es aceptada."""
        cfg = None
        ruta = _escribir_ini(_INI_VALIDO.replace("selecciones = clipboard,primary", "selecciones = primary"))
        try:
            cfg = cargar_config(ruta)
        finally:
            ruta.unlink(missing_ok=True)
        self.assertEqual(cfg.salida.selecciones, ["primary"])


class TestFicheroInexistente(unittest.TestCase):
    """El fichero de config ausente debe lanzar ConfigError, no FileNotFoundError."""

    def test_ruta_inexistente(self):
        with self.assertRaises(ConfigError) as ctx:
            cargar_config(Path("/tmp/no_existe_nunca_12345.ini"))
        err = ctx.exception
        self.assertEqual(err.campo, "ruta")
        self.assertIn("no encontrado", err.motivo)


class TestExpansionPid(unittest.TestCase):
    """La ruta del PID expande $XDG_RUNTIME_DIR o cae en /tmp si no está definido."""

    def _ini_con_xdg(self) -> Path:
        return _escribir_ini(
            _INI_VALIDO.replace("fichero_pid = /tmp/evp.pid", "fichero_pid = $XDG_RUNTIME_DIR/evp.pid")
        )

    def test_expansion_xdg_definido(self):
        directorio = "/run/user/9999"
        ruta = self._ini_con_xdg()
        anterior = os.environ.get("XDG_RUNTIME_DIR")
        try:
            os.environ["XDG_RUNTIME_DIR"] = directorio
            cfg = cargar_config(ruta)
            self.assertEqual(cfg.general.fichero_pid, f"{directorio}/evp.pid")
        finally:
            if anterior is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = anterior
            ruta.unlink(missing_ok=True)

    def test_expansion_xdg_no_definido(self):
        """Sin XDG_RUNTIME_DIR en el entorno, debe sustituir por /tmp."""
        ruta = self._ini_con_xdg()
        anterior = os.environ.pop("XDG_RUNTIME_DIR", None)
        try:
            cfg = cargar_config(ruta)
            self.assertEqual(cfg.general.fichero_pid, "/tmp/evp.pid")
        finally:
            if anterior is not None:
                os.environ["XDG_RUNTIME_DIR"] = anterior
            ruta.unlink(missing_ok=True)


class TestRutaPorDefecto(unittest.TestCase):
    """La función auxiliar debe apuntar a config/config.ini en la raíz del repo."""

    def test_nombre_y_directorio(self):
        ruta = ruta_config_por_defecto()
        self.assertEqual(ruta.name, "config.ini")
        self.assertEqual(ruta.parent.name, "config")

    def test_ruta_existe(self):
        # El fichero debe existir en el repo después de esta tarea
        self.assertTrue(ruta_config_por_defecto().exists())


if __name__ == "__main__":
    unittest.main()
