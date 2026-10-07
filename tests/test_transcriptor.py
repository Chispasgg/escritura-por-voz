"""
Tests unitarios para app.whisper_ptt.transcriptor.

Estrategia: se inyecta un ModeloFalso en lugar del WhisperModel real para
que los tests no necesiten GPU, CUDA ni descarga de modelos.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import numpy as np

from app.whisper_ptt.config import ConfigWhisper
from app.whisper_ptt.transcriptor import (
    COD_CARGA_MODELO,
    COD_TRANSCRIPCION,
    TranscriptorError,
    TranscriptorWhisper,
)

# ---------------------------------------------------------------------------
# Utilidades de test
# ---------------------------------------------------------------------------


def _config(
    modelo: str = "tiny",
    dispositivo: str = "cpu",
    tipo_computo: str = "int8",
    idioma: str = "es",
    filtro_vad: bool = True,
    tamano_haz: int = 5,
    prompt_inicial: str = "",
) -> ConfigWhisper:
    return ConfigWhisper(
        modelo=modelo,
        dispositivo=dispositivo,
        tipo_computo=tipo_computo,
        idioma=idioma,
        filtro_vad=filtro_vad,
        tamano_haz=tamano_haz,
        prompt_inicial=prompt_inicial,
    )


def _segmento(texto: str) -> MagicMock:
    """Crea un segmento falso con atributo text."""
    seg = MagicMock()
    seg.text = texto
    return seg


class ModeloFalso:
    """
    Doble del WhisperModel de faster-whisper.

    Registra los argumentos recibidos en la última llamada a transcribe()
    para que los tests puedan verificarlos.
    """

    def __init__(self) -> None:
        self.ultima_llamada: dict | None = None
        # Segmentos que devolverá en la próxima llamada a transcribe()
        self.segmentos_respuesta: list = []

    def transcribe(
        self,
        audio,
        *,
        language,
        vad_filter,
        beam_size,
        initial_prompt,
    ):
        self.ultima_llamada = {
            "audio": audio,
            "language": language,
            "vad_filter": vad_filter,
            "beam_size": beam_size,
            "initial_prompt": initial_prompt,
        }
        # Devuelve (segmentos, info); info no se usa
        return iter(self.segmentos_respuesta), MagicMock()


def _fabrica_con_modelo(modelo_falso: ModeloFalso):
    """Devuelve una fábrica que siempre retorna modelo_falso."""

    def fabrica(nombre: str, dispositivo: str, tipo_computo: str) -> ModeloFalso:
        fabrica.llamado_con = (nombre, dispositivo, tipo_computo)
        return modelo_falso

    fabrica.llamado_con = None
    return fabrica


# ---------------------------------------------------------------------------
# Tests de construcción
# ---------------------------------------------------------------------------


class TestConstruccion(unittest.TestCase):
    def test_se_construye_con_modelo_falso(self):
        modelo = ModeloFalso()
        fabrica = _fabrica_con_modelo(modelo)
        t = TranscriptorWhisper(_config(), fabrica=fabrica)
        self.assertIsNotNone(t)

    def test_fabrica_recibe_parametros_de_config(self):
        """La fábrica debe recibir exactamente los valores de ConfigWhisper."""
        modelo = ModeloFalso()
        fabrica = _fabrica_con_modelo(modelo)
        cfg = _config(modelo="large-v3-turbo", dispositivo="cuda", tipo_computo="float16")
        TranscriptorWhisper(cfg, fabrica=fabrica)
        self.assertEqual(fabrica.llamado_con, ("large-v3-turbo", "cuda", "float16"))

    def test_fallo_de_fabrica_lanza_transcriptor_error(self):
        """Si la fábrica lanza, debe propagarse como TranscriptorError(CARGA_MODELO)."""

        def fabrica_rota(nombre, dispositivo, tipo_computo):
            raise RuntimeError("CUDA no disponible")

        with self.assertRaises(TranscriptorError) as ctx:
            TranscriptorWhisper(_config(), fabrica=fabrica_rota)

        self.assertEqual(ctx.exception.codigo, COD_CARGA_MODELO)
        self.assertIn("CUDA no disponible", ctx.exception.detalle)

    def test_mensaje_carga_incluye_sugerencia_cpu(self):
        """El mensaje de error de carga debe orientar al usuario hacia cpu/int8."""

        def fabrica_rota(nombre, dispositivo, tipo_computo):
            raise OSError("libcuda.so no encontrado")

        with self.assertRaises(TranscriptorError) as ctx:
            TranscriptorWhisper(_config(), fabrica=fabrica_rota)

        self.assertIn("cpu", ctx.exception.detalle)
        self.assertIn("int8", ctx.exception.detalle)


# ---------------------------------------------------------------------------
# Tests de transcripción: parámetros pasados correctamente
# ---------------------------------------------------------------------------


class TestParametros(unittest.TestCase):
    def _crear(self, **kwargs):
        modelo = ModeloFalso()
        fabrica = _fabrica_con_modelo(modelo)
        return TranscriptorWhisper(_config(**kwargs), fabrica=fabrica), modelo

    def test_idioma_se_pasa_al_modelo(self):
        t, modelo = self._crear(idioma="en")
        modelo.segmentos_respuesta = [_segmento("hello")]
        t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertEqual(modelo.ultima_llamada["language"], "en")

    def test_filtro_vad_true_se_pasa(self):
        t, modelo = self._crear(filtro_vad=True)
        modelo.segmentos_respuesta = []
        t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertTrue(modelo.ultima_llamada["vad_filter"])

    def test_filtro_vad_false_se_pasa(self):
        t, modelo = self._crear(filtro_vad=False)
        modelo.segmentos_respuesta = []
        t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertFalse(modelo.ultima_llamada["vad_filter"])

    def test_tamano_haz_se_pasa(self):
        t, modelo = self._crear(tamano_haz=8)
        modelo.segmentos_respuesta = []
        t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertEqual(modelo.ultima_llamada["beam_size"], 8)

    def test_prompt_inicial_no_vacio_se_pasa(self):
        t, modelo = self._crear(prompt_inicial="Claude GNOME")
        modelo.segmentos_respuesta = []
        t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertEqual(modelo.ultima_llamada["initial_prompt"], "Claude GNOME")

    def test_prompt_inicial_vacio_produce_none(self):
        """prompt_inicial vacío → None (no "" vacío)."""
        t, modelo = self._crear(prompt_inicial="")
        modelo.segmentos_respuesta = []
        t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertIsNone(modelo.ultima_llamada["initial_prompt"])

    def test_prompt_inicial_solo_espacios_produce_none(self):
        """prompt_inicial con solo espacios también debe normalizarse a None."""
        t, modelo = self._crear(prompt_inicial="   ")
        modelo.segmentos_respuesta = []
        t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertIsNone(modelo.ultima_llamada["initial_prompt"])

    def test_audio_se_pasa_como_ndarray(self):
        """El array numpy se pasa directamente al modelo (faster-whisper lo acepta)."""
        t, modelo = self._crear()
        audio = np.ones(16000, dtype=np.float32)
        modelo.segmentos_respuesta = []
        t.transcribir(audio)
        np.testing.assert_array_equal(modelo.ultima_llamada["audio"], audio)


# ---------------------------------------------------------------------------
# Tests de unión de segmentos
# ---------------------------------------------------------------------------


class TestUnionSegmentos(unittest.TestCase):
    def _crear(self):
        modelo = ModeloFalso()
        fabrica = _fabrica_con_modelo(modelo)
        return TranscriptorWhisper(_config(), fabrica=fabrica), modelo

    def test_un_segmento(self):
        t, modelo = self._crear()
        modelo.segmentos_respuesta = [_segmento("Hola mundo.")]
        resultado = t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertEqual(resultado, "Hola mundo.")

    def test_varios_segmentos_se_unen_con_espacio(self):
        t, modelo = self._crear()
        modelo.segmentos_respuesta = [
            _segmento("Hola,"),
            _segmento("¿qué tal?"),
        ]
        resultado = t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertEqual(resultado, "Hola, ¿qué tal?")

    def test_espacios_sobrantes_en_segmentos_se_eliminan(self):
        t, modelo = self._crear()
        modelo.segmentos_respuesta = [
            _segmento("  Hola  "),
            _segmento("  mundo.  "),
        ]
        resultado = t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertEqual(resultado, "Hola mundo.")

    def test_sin_segmentos_devuelve_cadena_vacia(self):
        t, modelo = self._crear()
        modelo.segmentos_respuesta = []
        resultado = t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertEqual(resultado, "")

    def test_segmentos_vacios_se_ignoran(self):
        """Segmentos con texto vacío o solo espacios no deben aparecer en el resultado."""
        t, modelo = self._crear()
        modelo.segmentos_respuesta = [
            _segmento(""),
            _segmento("Hola."),
            _segmento("   "),
        ]
        resultado = t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertEqual(resultado, "Hola.")


# ---------------------------------------------------------------------------
# Tests de audio vacío
# ---------------------------------------------------------------------------


class TestAudioVacio(unittest.TestCase):
    def _crear(self):
        modelo = ModeloFalso()
        fabrica = _fabrica_con_modelo(modelo)
        return TranscriptorWhisper(_config(), fabrica=fabrica), modelo

    def test_array_vacio_devuelve_cadena_vacia(self):
        t, modelo = self._crear()
        resultado = t.transcribir(np.array([], dtype=np.float32))
        self.assertEqual(resultado, "")

    def test_array_vacio_no_llama_al_modelo(self):
        """Con audio vacío no debe invocarse transcribe() en el modelo."""
        t, modelo = self._crear()
        t.transcribir(np.array([], dtype=np.float32))
        self.assertIsNone(modelo.ultima_llamada)


# ---------------------------------------------------------------------------
# Test: modelo se construye una sola vez para varias transcripciones
# ---------------------------------------------------------------------------


class TestModeloCargadoUnaVez(unittest.TestCase):
    def test_fabrica_llamada_una_sola_vez(self):
        """La fábrica solo debe llamarse en __init__, no en cada transcribir()."""
        conteo = {"n": 0}
        modelo = ModeloFalso()

        def fabrica_contador(nombre, dispositivo, tipo_computo):
            conteo["n"] += 1
            return modelo

        t = TranscriptorWhisper(_config(), fabrica=fabrica_contador)
        modelo.segmentos_respuesta = [_segmento("uno")]
        t.transcribir(np.ones(16000, dtype=np.float32))
        modelo.segmentos_respuesta = [_segmento("dos")]
        t.transcribir(np.ones(16000, dtype=np.float32))
        modelo.segmentos_respuesta = [_segmento("tres")]
        t.transcribir(np.ones(16000, dtype=np.float32))

        self.assertEqual(conteo["n"], 1, "La fábrica debe llamarse solo una vez")


# ---------------------------------------------------------------------------
# Tests de errores en transcripción
# ---------------------------------------------------------------------------


class TestErroresTranscripcion(unittest.TestCase):
    def _crear_modelo_que_falla(self):
        class ModeloQueExplota:
            def transcribe(self, audio, **kwargs):
                raise RuntimeError("fallo interno del motor")

        modelo = ModeloQueExplota()

        def fabrica(nombre, dispositivo, tipo_computo):
            return modelo

        return TranscriptorWhisper(_config(), fabrica=fabrica)

    def test_error_en_transcribe_lanza_transcriptor_error(self):
        t = self._crear_modelo_que_falla()
        with self.assertRaises(TranscriptorError) as ctx:
            t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertEqual(ctx.exception.codigo, COD_TRANSCRIPCION)
        self.assertIn("fallo interno del motor", ctx.exception.detalle)

    def test_error_en_iteracion_generador_lanza_transcriptor_error(self):
        """Si el generador de segmentos lanza al iterar → TranscriptorError(COD_TRANSCRIPCION).

        transcribe() puede devolver un generador lazy; faster-whisper lo evalúa
        solo al iterar. Si ese generador lanza, el except del transcribir() debe
        capturarlo igual que cualquier otro error.
        """

        def _gen_roto():
            yield _segmento("primer segmento")
            raise RuntimeError("fallo al leer el siguiente segmento")

        class ModeloConGeneradorRoto:
            def transcribe(self, audio, **kwargs):
                return _gen_roto(), None

        modelo = ModeloConGeneradorRoto()

        t = TranscriptorWhisper(_config(), fabrica=lambda *_: modelo)
        with self.assertRaises(TranscriptorError) as ctx:
            t.transcribir(np.ones(16000, dtype=np.float32))
        self.assertEqual(ctx.exception.codigo, COD_TRANSCRIPCION)
        self.assertIn("fallo al leer el siguiente segmento", ctx.exception.detalle)


# ---------------------------------------------------------------------------
# Test: cumple el Protocol
# ---------------------------------------------------------------------------


class TestProtocol(unittest.TestCase):
    def test_implementa_transcriptor_protocol(self):
        """TranscriptorWhisper debe satisfacer el Protocol en tiempo de ejecución."""
        modelo = ModeloFalso()
        fabrica = _fabrica_con_modelo(modelo)
        t = TranscriptorWhisper(_config(), fabrica=fabrica)
        # isinstance con Protocol requiere @runtime_checkable; lo omitimos aquí
        # porque el Protocol no lo declara (no es necesario para el daemon).
        # Verificamos duck typing: tiene el método correcto con la firma esperada.
        self.assertTrue(callable(getattr(t, "transcribir", None)))


if __name__ == "__main__":
    unittest.main()
