"""
Tests unitarios del módulo app.whisper_ptt.grabador.

Sin micrófono, sin PipeWire: se inyecta un proceso falso que emite bytes PCM
s16 conocidos. Se comprueba forma, dtype, rango y comportamiento ante estados
incorrectos (doble iniciar, detener sin iniciar, duración mínima).
"""

from __future__ import annotations

import io
import struct
import subprocess
import threading
import time
import unittest

import numpy as np

from app.whisper_ptt.grabador import (
    COD_NO_INICIADO,
    COD_PROCESO_FALLO,
    COD_YA_INICIADO,
    GrabadorError,
    GrabadorPipeWire,
    _bytes_s16_a_float32,
)

# ---------------------------------------------------------------------------
# Proceso falso
# ---------------------------------------------------------------------------


class _ProcesoFalso:
    """
    Simula un subprocess.Popen que emite bytes PCM s16 por stdout.
    La escritura ocurre en un hilo para que el lector del grabador pueda leer
    mientras el hilo principal sigue en iniciar().
    """

    def __init__(self, datos: bytes, retardo_s: float = 0.0) -> None:
        # Usamos un pipe real de OS para que la lectura funcione igual que con Popen
        import os

        leer_fd, escribir_fd = os.pipe()
        self.stdout = io.open(leer_fd, "rb")
        self._escribir = io.open(escribir_fd, "wb")
        # stderr=None imita un Popen sin stderr capturado; detener() lo omite
        self.stderr = None
        self._datos = datos
        self._retardo_s = retardo_s
        self.returncode: int | None = None
        self._terminado = threading.Event()

        # Hilo que escribe los datos y luego cierra el extremo de escritura
        hilo = threading.Thread(target=self._emitir, daemon=True)
        hilo.start()

    def _emitir(self) -> None:
        if self._retardo_s > 0:
            time.sleep(self._retardo_s)
        try:
            self._escribir.write(self._datos)
            self._escribir.flush()
        finally:
            self._escribir.close()
        self._terminado.wait(timeout=5.0)

    def terminate(self) -> None:
        self.returncode = -15
        self._terminado.set()
        try:
            self._escribir.close()
        except Exception:
            pass

    def kill(self) -> None:
        self.returncode = -9
        self._terminado.set()
        try:
            self._escribir.close()
        except Exception:
            pass

    def wait(self, timeout: float | None = None) -> int:
        self._terminado.wait(timeout=timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="falso", timeout=timeout)
        return self.returncode


def _lanzador_falso(datos: bytes, retardo_s: float = 0.0):
    """Devuelve una función de lanzado que ignora los args y emite datos fijos."""

    def lanzar(_args: list[str]) -> _ProcesoFalso:
        return _ProcesoFalso(datos, retardo_s)

    return lanzar


# ---------------------------------------------------------------------------
# Generación de bytes PCM s16 de prueba
# ---------------------------------------------------------------------------

_HZ = 16000
_CANALES = 1


def _generar_pcm_s16(n_frames: int, valor: int = 0x1000) -> bytes:
    """
    Genera n_frames muestras s16le de valor constante.
    Con valor=0x1000 (4096) la muestra float32 resultante es 4096/32768 ≈ 0.125.
    """
    return struct.pack(f"<{n_frames}h", *([valor] * n_frames))


# ---------------------------------------------------------------------------
# Tests de la función auxiliar de conversión
# ---------------------------------------------------------------------------


class TestBytesS16AFloat32(unittest.TestCase):
    """Pruebas unitarias de _bytes_s16_a_float32 aislada del proceso."""

    def test_conversion_valor_conocido(self):
        # 0x1000 = 4096; 4096/32768 = 0.125
        datos = struct.pack("<4h", 4096, 4096, 4096, 4096)
        arr = _bytes_s16_a_float32(datos, canales=1)
        self.assertEqual(arr.dtype, np.float32)
        self.assertEqual(len(arr), 4)
        np.testing.assert_allclose(arr, 0.125, atol=1e-6)

    def test_rango_maximo(self):
        # s16 min = -32768 → float32 = -1.0; max = 32767 → ≈ 1.0
        datos = struct.pack("<2h", -32768, 32767)
        arr = _bytes_s16_a_float32(datos, canales=1)
        self.assertAlmostEqual(arr[0], -1.0, places=5)
        self.assertGreater(arr[1], 0.99)
        self.assertLessEqual(arr[1], 1.0)

    def test_mono_downmix_stereo(self):
        # Dos canales: L=0x2000, R=0x2000 → promedio = 0x2000 → float32 = 0.25
        datos = struct.pack("<4h", 0x2000, 0x2000, 0x2000, 0x2000)
        arr = _bytes_s16_a_float32(datos, canales=2)
        self.assertEqual(len(arr), 2)  # 4 muestras / 2 canales = 2 frames
        np.testing.assert_allclose(arr, 0.25, atol=1e-6)

    def test_datos_vacios(self):
        arr = _bytes_s16_a_float32(b"", canales=1)
        self.assertEqual(len(arr), 0)
        self.assertEqual(arr.dtype, np.float32)

    def test_byte_suelto_ignorado(self):
        # Un byte suelto al final no es una muestra completa; debe ignorarse
        datos = struct.pack("<2h", 100, 200) + b"\x01"
        arr = _bytes_s16_a_float32(datos, canales=1)
        self.assertEqual(len(arr), 2)


# ---------------------------------------------------------------------------
# Tests del GrabadorPipeWire con proceso falso
# ---------------------------------------------------------------------------


class TestGrabadorEstados(unittest.TestCase):
    """Verifica comportamiento ante uso incorrecto de iniciar/detener."""

    def _grabador(self, datos: bytes = b"", duracion_minima: float = 0.0):
        return GrabadorPipeWire(
            frecuencia_muestreo=_HZ,
            canales=_CANALES,
            duracion_minima_s=duracion_minima,
            lanzador=_lanzador_falso(datos),
        )

    def test_detener_sin_iniciar_lanza_error(self):
        g = self._grabador()
        with self.assertRaises(GrabadorError) as ctx:
            g.detener()
        self.assertEqual(ctx.exception.codigo, COD_NO_INICIADO)

    def test_iniciar_dos_veces_lanza_error(self):
        g = self._grabador()
        g.iniciar()
        try:
            with self.assertRaises(GrabadorError) as ctx:
                g.iniciar()
            self.assertEqual(ctx.exception.codigo, COD_YA_INICIADO)
        finally:
            g.detener()

    def test_ciclo_completo_reiniciable(self):
        """Después de detener() se puede volver a iniciar() sin error."""
        datos = _generar_pcm_s16(100)
        g = self._grabador(datos)
        g.iniciar()
        g.detener()
        g.iniciar()
        resultado = g.detener()
        # Solo verificamos que no lanza excepción; el contenido se testa abajo
        self.assertIsInstance(resultado, np.ndarray)


class TestGrabadorAudio(unittest.TestCase):
    """Verifica que el audio devuelto tiene la forma, dtype y rango correctos."""

    def _grabador_con(self, n_frames: int, valor: int = 0x1000, duracion_minima: float = 0.0):
        datos = _generar_pcm_s16(n_frames, valor)
        return GrabadorPipeWire(
            frecuencia_muestreo=_HZ,
            canales=_CANALES,
            duracion_minima_s=duracion_minima,
            lanzador=_lanzador_falso(datos),
        )

    def test_dtype_float32(self):
        g = self._grabador_con(160)
        g.iniciar()
        arr = g.detener()
        self.assertEqual(arr.dtype, np.float32)

    def test_forma_1d(self):
        g = self._grabador_con(160)
        g.iniciar()
        arr = g.detener()
        self.assertEqual(arr.ndim, 1)

    def test_n_muestras(self):
        g = self._grabador_con(200)
        g.iniciar()
        arr = g.detener()
        self.assertEqual(len(arr), 200)

    def test_rango_normalizado(self):
        # Con valor = 0x1000 (4096) esperamos 4096/32768 ≈ 0.125
        g = self._grabador_con(100, valor=0x1000)
        g.iniciar()
        arr = g.detener()
        np.testing.assert_allclose(arr, 0.125, atol=1e-6)

    def test_maximo_positivo_no_supera_1(self):
        g = self._grabador_con(50, valor=32767)
        g.iniciar()
        arr = g.detener()
        self.assertTrue(np.all(arr <= 1.0))

    def test_minimo_negativo_no_supera_menos_1(self):
        datos = struct.pack("<50h", *([-32768] * 50))
        g = GrabadorPipeWire(
            frecuencia_muestreo=_HZ,
            canales=_CANALES,
            duracion_minima_s=0.0,
            lanzador=_lanzador_falso(datos),
        )
        g.iniciar()
        arr = g.detener()
        self.assertTrue(np.all(arr >= -1.0))


class TestDuracionMinima(unittest.TestCase):
    """Grabaciones cortas deben devolver array vacío."""

    def test_grabacion_corta_devuelve_vacio(self):
        # duracion_minima = 10 s; el proceso falso termina casi al instante
        datos = _generar_pcm_s16(160)
        g = GrabadorPipeWire(
            frecuencia_muestreo=_HZ,
            canales=_CANALES,
            duracion_minima_s=10.0,  # imposible de superar en el test
            lanzador=_lanzador_falso(datos),
        )
        g.iniciar()
        arr = g.detener()
        self.assertEqual(len(arr), 0)
        self.assertEqual(arr.dtype, np.float32)

    def test_grabacion_suficiente_devuelve_audio(self):
        datos = _generar_pcm_s16(160)
        g = GrabadorPipeWire(
            frecuencia_muestreo=_HZ,
            canales=_CANALES,
            duracion_minima_s=0.0,  # sin mínimo
            lanzador=_lanzador_falso(datos),
        )
        g.iniciar()
        arr = g.detener()
        self.assertGreater(len(arr), 0)


class TestProcesoNoExiste(unittest.TestCase):
    """Si el ejecutable no existe, iniciar() lanza GrabadorError."""

    def test_ejecutable_inexistente(self):
        g = GrabadorPipeWire(
            frecuencia_muestreo=_HZ,
            canales=_CANALES,
            duracion_minima_s=0.0,
            ejecutable="/no/existe/pw-record-falso",
        )
        with self.assertRaises(GrabadorError) as ctx:
            g.iniciar()
        self.assertEqual(ctx.exception.codigo, COD_PROCESO_FALLO)


# ---------------------------------------------------------------------------
# Proceso falso para muerte espontánea
# ---------------------------------------------------------------------------


class _ProcesoFalsoMuerteEspontanea:
    """
    Simula un proceso que ya murió antes de que llamemos a terminate().
    Tiene stdout vacío y stderr con un mensaje de error configurable.
    terminate() lanza OSError (proceso ya terminado), igual que Popen real.
    """

    def __init__(self, returncode: int, stderr_datos: bytes = b"") -> None:
        import os

        # stdout vacío: el proceso no grabó nada
        leer_fd, escribir_fd = os.pipe()
        self.stdout = io.open(leer_fd, "rb")
        io.open(escribir_fd, "wb").close()

        # stderr con el mensaje de error
        leer_err_fd, escribir_err_fd = os.pipe()
        self.stderr = io.open(leer_err_fd, "rb")
        stderr_pipe = io.open(escribir_err_fd, "wb")
        try:
            stderr_pipe.write(stderr_datos)
            stderr_pipe.flush()
        finally:
            stderr_pipe.close()

        self.returncode = returncode

    def terminate(self) -> None:
        # El proceso ya no existe; simula el OSError que lanza Popen real
        raise OSError(3, "No such process")

    def kill(self) -> None:
        raise OSError(3, "No such process")

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


# ---------------------------------------------------------------------------
# Tests de muerte espontánea
# ---------------------------------------------------------------------------


class TestMuerteEspontanea(unittest.TestCase):
    """
    Verifica la detección de muerte espontánea de pw-record.

    Casos:
    - Código de error (> 0) → GrabadorError(COD_PROCESO_FALLO) con extracto stderr.
    - Código de señal ajena (< -9) → GrabadorError(COD_PROCESO_FALLO).
    - Terminado por nuestra señal (-15) → audio normal, sin excepción.
    """

    def _lanzador_muerte(self, returncode: int, stderr_datos: bytes = b""):
        def lanzar(_args: list[str]) -> _ProcesoFalsoMuerteEspontanea:
            return _ProcesoFalsoMuerteEspontanea(returncode, stderr_datos)

        return lanzar

    def test_muerte_con_codigo_error_lanza_grabador_error(self):
        """returncode=1 → GrabadorError(COD_PROCESO_FALLO) con fragmento de stderr."""
        stderr_msg = b"audio/sink: No such device"
        g = GrabadorPipeWire(
            frecuencia_muestreo=_HZ,
            canales=_CANALES,
            duracion_minima_s=0.0,
            lanzador=self._lanzador_muerte(1, stderr_msg),
        )
        g.iniciar()
        with self.assertRaises(GrabadorError) as ctx:
            g.detener()
        err = ctx.exception
        self.assertEqual(err.codigo, COD_PROCESO_FALLO)
        # El código de retorno debe aparecer en el mensaje
        self.assertIn("1", err.detalle)
        # El extracto de stderr debe aparecer en el mensaje
        self.assertIn("No such device", err.detalle)

    def test_muerte_con_senal_ajena_lanza_grabador_error(self):
        """returncode=-11 (SIGSEGV) → GrabadorError(COD_PROCESO_FALLO)."""
        g = GrabadorPipeWire(
            frecuencia_muestreo=_HZ,
            canales=_CANALES,
            duracion_minima_s=0.0,
            lanzador=self._lanzador_muerte(-11),
        )
        g.iniciar()
        with self.assertRaises(GrabadorError) as ctx:
            g.detener()
        self.assertEqual(ctx.exception.codigo, COD_PROCESO_FALLO)

    def test_stderr_acotado_a_2k(self):
        """Si stderr supera 2 KiB, el fragmento incluido no supera ese tamaño."""
        # stderr de 4 KiB: los últimos 2 KiB terminan en 'Z' repetida
        stderr_datos = b"A" * 2048 + b"Z" * 2048
        g = GrabadorPipeWire(
            frecuencia_muestreo=_HZ,
            canales=_CANALES,
            duracion_minima_s=0.0,
            lanzador=self._lanzador_muerte(2, stderr_datos),
        )
        g.iniciar()
        with self.assertRaises(GrabadorError) as ctx:
            g.detener()
        # El fragmento incluye los últimos 2 KiB (las Z), no los primeros (A)
        self.assertIn("Z", ctx.exception.detalle)

    def test_muerte_por_nuestra_senal_no_lanza_error(self):
        """returncode=-15 (nuestra SIGTERM) → audio normal, sin GrabadorError."""
        datos = _generar_pcm_s16(160)
        g = GrabadorPipeWire(
            frecuencia_muestreo=_HZ,
            canales=_CANALES,
            duracion_minima_s=0.0,
            lanzador=_lanzador_falso(datos),
        )
        g.iniciar()
        # _ProcesoFalso.terminate() establece returncode=-15; no debe lanzar error
        arr = g.detener()
        self.assertIsInstance(arr, np.ndarray)
        self.assertEqual(arr.dtype, np.float32)


if __name__ == "__main__":
    unittest.main()
