"""
Tests unitarios del módulo app.whisper_ptt.daemon.

Sin X11, GPU ni micrófono: se usan dobles para las cuatro dependencias
(GrabadorProtocol, TranscriptorProtocol, Salida, GestorHotkey).

Sincronización determinista:
  - _TranscriptorDoble.esperar_transcripcion(n): espera hasta que
    transcribir() haya completado (incluyendo cuando lanza) n veces.
  - _SalidaDoble.esperar(n): espera n escrituras exitosas.
  - _SalidaDoble.esperar_llamada(n): espera n llamadas totales,
    incluyendo las que lanzan error.
  - _GestorDoble._escuchando: Event que se activa en cuanto escuchar()
    entra en el bucle, garantizando que el PID ya está creado.
  No se usa time.sleep() en ningún test; toda espera tiene timeout
  explícito que fallará el test si el daemon no responde a tiempo.

Cobertura:
  - Flujo pulsar → soltar → transcribir → pegar.
  - Audio vacío (grabación demasiado corta): se descarta sin transcribir.
  - Transcripción vacía: se descarta sin pegar.
  - Error en el grabador: se registra, daemon sigue vivo.
  - Error en el transcriptor: se registra, daemon sigue vivo.
  - Error en la salida: se registra, daemon sigue vivo.
  - Orden FIFO con varias pulsaciones consecutivas.
  - Fichero PID: creación, PID obsoleto reemplazado, PID vivo → excepción,
    borrado al salir.
  - Parada limpia: hilo trabajador termina, PID borrado.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from app.whisper_ptt.daemon import (
    Daemon,
    DaemonYaEnMarcha,
    _borrar_pid,
    _crear_pid,
)
from app.whisper_ptt.grabador import (
    COD_NO_INICIADO,
    COD_YA_INICIADO,
    GrabadorError,
)
from app.whisper_ptt.salida import SalidaError
from app.whisper_ptt.transcriptor import COD_TRANSCRIPCION, TranscriptorError

# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class _GrabadorDoble:
    """
    Grabador doble configurable.

    Se controla qué devuelve detener() y si iniciar()/detener() lanzan error.
    """

    def __init__(
        self,
        audio: np.ndarray | None = None,
        error_iniciar: GrabadorError | None = None,
        error_detener: GrabadorError | None = None,
    ) -> None:
        self._audio = audio if audio is not None else _audio(0.5)
        self._error_iniciar = error_iniciar
        self._error_detener = error_detener
        self.llamadas_iniciar: int = 0
        self.llamadas_detener: int = 0

    def iniciar(self) -> None:
        self.llamadas_iniciar += 1
        if self._error_iniciar is not None:
            raise self._error_iniciar

    def detener(self) -> np.ndarray:
        self.llamadas_detener += 1
        if self._error_detener is not None:
            raise self._error_detener
        return self._audio


class _TranscriptorDoble:
    """
    Transcriptor doble: devuelve textos predefinidos en orden.

    Notificación determinista: esperar_transcripcion(n) bloquea hasta que
    transcribir() haya completado n veces (incluyendo las llamadas que
    lanzan error).
    """

    def __init__(
        self,
        textos: list[str] | None = None,
        error: TranscriptorError | None = None,
    ) -> None:
        self._textos = textos if textos is not None else ["hola mundo"]
        self._error = error
        self._indice = 0
        self.llamadas: list[np.ndarray] = []
        self._cv = threading.Condition()
        self._n_completadas = 0

    def transcribir(self, audio: np.ndarray) -> str:
        self.llamadas.append(audio)
        try:
            if self._error is not None:
                raise self._error
            texto = self._textos[min(self._indice, len(self._textos) - 1)]
            self._indice += 1
            return texto
        finally:
            # Notificar siempre, incluso al lanzar, para que esperar_transcripcion()
            # pueda usarse tanto en casos de éxito como de error.
            with self._cv:
                self._n_completadas += 1
                self._cv.notify_all()

    def esperar_transcripcion(self, n: int = 1, timeout: float = 2.0) -> bool:
        """Retorna True si transcribir() completó al menos n veces antes del timeout."""
        limite = time.monotonic() + timeout
        with self._cv:
            while self._n_completadas < n:
                restante = limite - time.monotonic()
                if restante <= 0:
                    return False
                self._cv.wait(timeout=restante)
            return True


class _SalidaDoble:
    """
    Salida doble: acumula los textos escritos y notifica por Condition.

    esperar(n, timeout): espera n escrituras exitosas.
    esperar_llamada(n, timeout): espera n llamadas totales, incluyendo
        las que lanzan error. Úsalo para sincronizar con el hilo trabajador
        en tests que verifican robustez ante errores de salida.
    """

    def __init__(self, error: SalidaError | None = None) -> None:
        self._error = error
        self.textos: list[str] = []
        self._cv = threading.Condition()
        self._n_llamadas = 0  # total de llamadas (éxito + error)

    def escribir(self, texto: str) -> None:
        # Notificar antes de lanzar para que esperar_llamada() funcione
        # también en el caso de error: el trabajador aún no ha procesado
        # el except, pero la llamada sí ha ocurrido.
        with self._cv:
            self._n_llamadas += 1
            if self._error is None:
                self.textos.append(texto)
            self._cv.notify_all()
        if self._error is not None:
            raise self._error

    def esperar(self, n: int = 1, timeout: float = 2.0) -> bool:
        """Retorna True si se recibieron al menos *n* escrituras exitosas."""
        limite = time.monotonic() + timeout
        with self._cv:
            while len(self.textos) < n:
                restante = limite - time.monotonic()
                if restante <= 0:
                    return False
                self._cv.wait(timeout=restante)
            return True

    def esperar_llamada(self, n: int = 1, timeout: float = 2.0) -> bool:
        """Retorna True si se produjeron al menos *n* llamadas (éxito o error)."""
        limite = time.monotonic() + timeout
        with self._cv:
            while self._n_llamadas < n:
                restante = limite - time.monotonic()
                if restante <= 0:
                    return False
                self._cv.wait(timeout=restante)
            return True


class _GestorDoble:
    """
    GestorHotkey doble: escuchar() se bloquea hasta que detener() sea llamado.

    _escuchando: Event que se activa en cuanto escuchar() entra en el
    bucle de espera. Como arrancar() crea el PID antes de llamar a
    escuchar(), esperar _escuchando garantiza que el PID ya existe.
    """

    def __init__(self, acciones_al_escuchar=None) -> None:
        self._parar = threading.Event()
        self._acciones = acciones_al_escuchar
        # Se activa al inicio de escuchar(), antes del bloqueo.
        self._escuchando = threading.Event()

    def escuchar(self) -> None:
        self._escuchando.set()
        if self._acciones is not None:
            self._acciones()
        self._parar.wait()

    def detener(self) -> None:
        self._parar.set()


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _audio(segundos: float = 0.5, fs: int = 16_000) -> np.ndarray:
    """Genera un array de ruido blanco float32 de la duración indicada."""
    n = int(segundos * fs)
    rng = np.random.default_rng(0)
    return rng.uniform(-0.1, 0.1, n).astype(np.float32)


def _iniciar_trabajador(daemon: Daemon) -> threading.Thread:
    """Arranca el hilo trabajador del daemon y lo devuelve."""
    hilo = threading.Thread(target=daemon._trabajador, daemon=True)
    daemon._hilo_trabajador = hilo
    hilo.start()
    return hilo


def _detener_trabajador(daemon: Daemon, timeout: float = 2.0) -> None:
    """Envía el centinela y espera que el trabajador termine."""
    daemon._stop.set()
    daemon._cola.put(None)
    if daemon._hilo_trabajador is not None:
        daemon._hilo_trabajador.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Tests de flujo principal
# ---------------------------------------------------------------------------


class TestFlujoPrincipal(unittest.TestCase):
    """Prueba el ciclo pulsar → soltar → transcribir → pegar."""

    def setUp(self) -> None:
        self.salida = _SalidaDoble()
        self.grabador = _GrabadorDoble(audio=_audio(0.5))
        self.transcriptor = _TranscriptorDoble(textos=["hola mundo"])
        self.daemon = Daemon(self.grabador, self.transcriptor, self.salida)
        _iniciar_trabajador(self.daemon)

    def tearDown(self) -> None:
        _detener_trabajador(self.daemon)

    def test_flujo_completo(self) -> None:
        """Pulsar → soltar → transcribir → pegar."""
        self.daemon.al_pulsar()
        self.daemon.al_soltar()

        ok = self.salida.esperar(1)
        self.assertTrue(ok, "El texto no llegó a la salida en el tiempo esperado.")
        self.assertEqual(self.salida.textos, ["hola mundo"])

    def test_iniciar_llamado_al_pulsar(self) -> None:
        self.daemon.al_pulsar()
        self.assertEqual(self.grabador.llamadas_iniciar, 1)

    def test_detener_llamado_al_soltar(self) -> None:
        self.daemon.al_pulsar()
        self.daemon.al_soltar()
        self.assertEqual(self.grabador.llamadas_detener, 1)


# ---------------------------------------------------------------------------
# Tests de audio vacío
# ---------------------------------------------------------------------------


class TestAudioVacio(unittest.TestCase):
    """Grabaciones demasiado cortas devuelven array vacío → se ignoran."""

    def setUp(self) -> None:
        self.salida = _SalidaDoble()
        self.grabador = _GrabadorDoble(audio=np.array([], dtype=np.float32))
        self.transcriptor = _TranscriptorDoble(textos=["no debería llegar"])
        self.daemon = Daemon(self.grabador, self.transcriptor, self.salida)
        _iniciar_trabajador(self.daemon)

    def tearDown(self) -> None:
        _detener_trabajador(self.daemon)

    def test_audio_vacio_no_transcribe(self) -> None:
        # al_soltar() detecta len(audio)==0 de forma síncrona y retorna
        # sin encolar nada. No hay nada que esperar: el trabajador no
        # recibe ningún ítem para este caso, así que comprobamos
        # directamente al retornar al_soltar().
        self.daemon.al_pulsar()
        self.daemon.al_soltar()

        self.assertEqual(self.transcriptor.llamadas, [], "No debería haber llamado al transcriptor.")
        self.assertEqual(self.salida.textos, [], "No debería haber pegado nada.")


# ---------------------------------------------------------------------------
# Tests de transcripción vacía
# ---------------------------------------------------------------------------


class TestTranscripcionVacia(unittest.TestCase):
    """Texto vacío devuelto por el transcriptor → no se pega nada."""

    def setUp(self) -> None:
        self.salida = _SalidaDoble()
        self.grabador = _GrabadorDoble(audio=_audio(0.5))
        self.transcriptor = _TranscriptorDoble(textos=[""])
        self.daemon = Daemon(self.grabador, self.transcriptor, self.salida)
        _iniciar_trabajador(self.daemon)

    def tearDown(self) -> None:
        _detener_trabajador(self.daemon)

    def test_texto_vacio_no_pega(self) -> None:
        self.daemon.al_pulsar()
        self.daemon.al_soltar()

        # Esperar a que el trabajador haya procesado el ítem antes de
        # comprobar que salida nunca fue llamada.
        ok = self.transcriptor.esperar_transcripcion(1)
        self.assertTrue(ok, "El trabajador no procesó el ítem a tiempo.")
        self.assertEqual(self.salida.textos, [], "No debería haber pegado nada con texto vacío.")


# ---------------------------------------------------------------------------
# Tests de robustez: errores en las piezas
# ---------------------------------------------------------------------------


class TestErrorGrabadorIniciar(unittest.TestCase):
    """Error en grabador.iniciar() → se registra, daemon sigue vivo."""

    def setUp(self) -> None:
        self.salida = _SalidaDoble()
        self.grabador = _GrabadorDoble(error_iniciar=GrabadorError(COD_YA_INICIADO, "ya grabando"))
        self.transcriptor = _TranscriptorDoble()
        self.daemon = Daemon(self.grabador, self.transcriptor, self.salida)
        _iniciar_trabajador(self.daemon)

    def tearDown(self) -> None:
        _detener_trabajador(self.daemon)

    def test_error_iniciar_no_tumba_daemon(self) -> None:
        # No debe lanzar excepción
        self.daemon.al_pulsar()
        self.daemon.al_pulsar()  # segunda llamada también gestionada
        # El daemon sigue vivo: el hilo trabajador no se ha detenido.
        self.assertTrue(
            self.daemon._hilo_trabajador is not None and self.daemon._hilo_trabajador.is_alive(),
            "El hilo trabajador no debería haberse detenido.",
        )


class TestErrorGrabadorDetener(unittest.TestCase):
    """Error en grabador.detener() → se registra, nada se encola."""

    def setUp(self) -> None:
        self.salida = _SalidaDoble()
        self.grabador = _GrabadorDoble(error_detener=GrabadorError(COD_NO_INICIADO, "sin grabación"))
        self.transcriptor = _TranscriptorDoble(textos=["no debería llegar"])
        self.daemon = Daemon(self.grabador, self.transcriptor, self.salida)
        _iniciar_trabajador(self.daemon)

    def tearDown(self) -> None:
        _detener_trabajador(self.daemon)

    def test_error_detener_no_encola(self) -> None:
        # al_soltar() captura el GrabadorError de forma síncrona y retorna
        # sin encolar nada. No hay ítem en la cola que esperar.
        self.daemon.al_pulsar()
        self.daemon.al_soltar()

        self.assertEqual(self.transcriptor.llamadas, [])
        self.assertEqual(self.salida.textos, [])


class TestErrorTranscriptor(unittest.TestCase):
    """Error en transcriptor.transcribir() → se registra, daemon sigue vivo."""

    def setUp(self) -> None:
        self.salida = _SalidaDoble()
        self.grabador = _GrabadorDoble(audio=_audio(0.5))
        self.transcriptor = _TranscriptorDoble(error=TranscriptorError(COD_TRANSCRIPCION, "error GPU"))
        self.daemon = Daemon(self.grabador, self.transcriptor, self.salida)
        _iniciar_trabajador(self.daemon)

    def tearDown(self) -> None:
        _detener_trabajador(self.daemon)

    def test_error_transcriptor_no_tumba_daemon(self) -> None:
        self.daemon.al_pulsar()
        self.daemon.al_soltar()

        # Esperar a que el trabajador haya procesado el ítem (y el error).
        ok = self.transcriptor.esperar_transcripcion(1)
        self.assertTrue(ok, "El trabajador no procesó el ítem a tiempo.")
        self.assertEqual(self.salida.textos, [], "Nada debería haberse pegado.")
        self.assertTrue(self.daemon._hilo_trabajador.is_alive())

    def test_error_transcriptor_daemon_sigue_procesando(self) -> None:
        """Tras un error, el daemon puede procesar la siguiente pulsación."""
        # Primera pulsación: error de transcripción.
        self.daemon.al_pulsar()
        self.daemon.al_soltar()
        # Esperar a que el trabajador complete la transcripción fallida antes
        # de cambiar el doble; así el segundo ítem usará el nuevo transcriptor.
        ok = self.transcriptor.esperar_transcripcion(1)
        self.assertTrue(ok, "El trabajador no procesó el primer ítem a tiempo.")

        # Segunda pulsación: sin error.
        self.daemon._transcriptor = _TranscriptorDoble(textos=["segunda pulsación"])
        self.daemon.al_pulsar()
        self.daemon.al_soltar()

        ok = self.salida.esperar(1)
        self.assertTrue(ok)
        self.assertEqual(self.salida.textos, ["segunda pulsación"])


class TestErrorSalida(unittest.TestCase):
    """Error en salida.escribir() → se registra, daemon sigue vivo."""

    def setUp(self) -> None:
        self.salida = _SalidaDoble(error=SalidaError("xdotool", "fallo"))
        self.grabador = _GrabadorDoble(audio=_audio(0.5))
        self.transcriptor = _TranscriptorDoble(textos=["texto"])
        self.daemon = Daemon(self.grabador, self.transcriptor, self.salida)
        _iniciar_trabajador(self.daemon)

    def tearDown(self) -> None:
        _detener_trabajador(self.daemon)

    def test_error_salida_no_tumba_daemon(self) -> None:
        self.daemon.al_pulsar()
        self.daemon.al_soltar()

        # Esperar a que el trabajador haya llamado a salida.escribir() (con error).
        ok = self.salida.esperar_llamada(1)
        self.assertTrue(ok, "El trabajador no llamó a salida a tiempo.")
        self.assertTrue(self.daemon._hilo_trabajador.is_alive())

    def test_error_salida_daemon_sigue_procesando(self) -> None:
        """Tras un error de salida, el daemon puede procesar la siguiente."""
        # Primera pulsación: error de salida.
        self.daemon.al_pulsar()
        self.daemon.al_soltar()
        # Esperar a que el trabajador haya completado la llamada fallida antes
        # de cambiar el doble de salida.
        ok = self.salida.esperar_llamada(1)
        self.assertTrue(ok, "El trabajador no llamó a salida a tiempo.")

        # Segunda pulsación: salida sin error.
        salida_ok = _SalidaDoble()
        self.daemon._salida = salida_ok
        self.daemon.al_pulsar()
        self.daemon.al_soltar()

        ok = salida_ok.esperar(1)
        self.assertTrue(ok)
        self.assertEqual(salida_ok.textos, ["texto"])


# ---------------------------------------------------------------------------
# Tests de orden FIFO con varias pulsaciones
# ---------------------------------------------------------------------------


class TestOrdenFIFO(unittest.TestCase):
    """
    Varias pulsaciones consecutivas se procesan en orden.
    El transcriptor devuelve un texto distinto para cada llamada.
    """

    N = 5  # número de pulsaciones

    def setUp(self) -> None:
        textos = [f"texto_{i}" for i in range(self.N)]
        self.salida = _SalidaDoble()
        self.grabador = _GrabadorDoble(audio=_audio(0.5))
        self.transcriptor = _TranscriptorDoble(textos=textos)
        self.daemon = Daemon(self.grabador, self.transcriptor, self.salida)
        _iniciar_trabajador(self.daemon)

    def tearDown(self) -> None:
        _detener_trabajador(self.daemon)

    def test_fifo(self) -> None:
        for _ in range(self.N):
            self.daemon.al_pulsar()
            self.daemon.al_soltar()

        ok = self.salida.esperar(self.N, timeout=5.0)
        self.assertTrue(ok, f"Solo llegaron {len(self.salida.textos)} de {self.N}.")
        self.assertEqual(
            self.salida.textos,
            [f"texto_{i}" for i in range(self.N)],
            "Los textos no llegaron en orden FIFO.",
        )


# ---------------------------------------------------------------------------
# Tests del fichero PID
# ---------------------------------------------------------------------------


class TestPID(unittest.TestCase):
    """Creación, PID obsoleto, PID vivo y borrado al salir."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.ruta = Path(self._tmpdir.name) / "test.pid"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_creacion_correcta(self) -> None:
        _crear_pid(self.ruta)
        self.assertTrue(self.ruta.exists())
        contenido = self.ruta.read_text().strip()
        self.assertEqual(contenido, str(os.getpid()))

    def test_pid_obsoleto_reemplazado(self) -> None:
        # PID 99999999 es casi con certeza inexistente.
        pid_muerto = 99_999_999
        self.ruta.write_text(str(pid_muerto))

        # _crear_pid debe limpiar el obsoleto y crear el nuestro.
        _crear_pid(self.ruta)
        contenido = self.ruta.read_text().strip()
        self.assertEqual(contenido, str(os.getpid()))

    def test_pid_vivo_lanza_excepcion(self) -> None:
        # Usamos el PID del proceso actual: sabemos que está vivo.
        self.ruta.write_text(str(os.getpid()))

        with self.assertRaises(DaemonYaEnMarcha) as ctx:
            _crear_pid(self.ruta)
        self.assertEqual(ctx.exception.pid, os.getpid())

    def test_borrar_pid_nuestro(self) -> None:
        _crear_pid(self.ruta)
        _borrar_pid(self.ruta)
        self.assertFalse(self.ruta.exists())

    def test_borrar_pid_ajeno_no_borra(self) -> None:
        """Si el fichero contiene un PID distinto al nuestro, no lo borramos."""
        self.ruta.write_text("9999")
        _borrar_pid(self.ruta)
        # El fichero debe seguir ahí.
        self.assertTrue(self.ruta.exists())

    def test_borrar_pid_inexistente_no_falla(self) -> None:
        """Borrar un PID que ya no existe no debe lanzar excepción."""
        _borrar_pid(self.ruta)  # El fichero no existe; no debe fallar.


# ---------------------------------------------------------------------------
# Tests de parada limpia
# ---------------------------------------------------------------------------


class TestParadaLimpia(unittest.TestCase):
    """
    arrancar() con un GestorDoble. Verifica que el PID se borra y el hilo
    trabajador termina.

    Sincronización: _GestorDoble._escuchando se activa en cuanto
    escuchar() entra en el bucle de espera. Como arrancar() crea el PID
    antes de llamar a escuchar(), esperar _escuchando garantiza que el
    PID ya existe en el momento de la comprobación.
    """

    def test_pid_borrado_al_salir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ruta_pid = Path(tmpdir) / "daemon.pid"
            salida = _SalidaDoble()
            grabador = _GrabadorDoble(audio=_audio(0.5))
            transcriptor = _TranscriptorDoble(textos=["hola"])
            daemon = Daemon(grabador, transcriptor, salida)
            gestor = _GestorDoble()

            hilo = threading.Thread(
                target=lambda: daemon.arrancar(gestor, ruta_pid=ruta_pid),
                daemon=True,
            )
            hilo.start()

            # Esperar determinísticamente a que el daemon haya creado el PID.
            self.assertTrue(
                gestor._escuchando.wait(timeout=2.0),
                "El daemon no entró en el bucle a tiempo.",
            )
            self.assertTrue(ruta_pid.exists(), "El PID debería existir durante la ejecución.")

            # Detener el gestor → arrancar() retorna → PID se borra.
            gestor.detener()
            hilo.join(timeout=3.0)

            self.assertFalse(ruta_pid.exists(), "El PID debería haberse borrado al salir.")

    def test_hilo_trabajador_termina(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ruta_pid = Path(tmpdir) / "daemon.pid"
            daemon = Daemon(
                _GrabadorDoble(),
                _TranscriptorDoble(),
                _SalidaDoble(),
            )
            gestor = _GestorDoble()

            hilo_arrancar = threading.Thread(
                target=lambda: daemon.arrancar(gestor, ruta_pid=ruta_pid),
                daemon=True,
            )
            hilo_arrancar.start()

            # Esperar a que el daemon esté en el bucle antes de detenerlo.
            self.assertTrue(
                gestor._escuchando.wait(timeout=2.0),
                "El daemon no entró en el bucle a tiempo.",
            )
            gestor.detener()
            hilo_arrancar.join(timeout=3.0)

            # El hilo trabajador debe haber terminado.
            self.assertIsNotNone(daemon._hilo_trabajador)
            self.assertFalse(
                daemon._hilo_trabajador.is_alive(),
                "El hilo trabajador debería haber terminado.",
            )

    def test_daemon_ya_en_marcha_lanza_excepcion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ruta_pid = Path(tmpdir) / "daemon.pid"
            # Crear el PID con nuestro propio PID para simular instancia viva.
            ruta_pid.write_text(str(os.getpid()))

            daemon = Daemon(
                _GrabadorDoble(),
                _TranscriptorDoble(),
                _SalidaDoble(),
            )
            gestor = _GestorDoble()

            with self.assertRaises(DaemonYaEnMarcha):
                daemon.arrancar(gestor, ruta_pid=ruta_pid)


# ---------------------------------------------------------------------------
# Tests de parada con grabación activa
# ---------------------------------------------------------------------------


class TestParadaConGrabacionActiva(unittest.TestCase):
    """
    Al recibir la señal de parada mientras hay una grabación en curso,
    _detener_limpiamente debe llamar a grabador.detener() sin fallar.
    """

    def test_grabador_detenido_en_limpieza(self) -> None:
        grabador = _GrabadorDoble(audio=_audio(0.5))
        daemon = Daemon(grabador, _TranscriptorDoble(), _SalidaDoble())

        # Simular que el grabador está activo: al_pulsar fue llamado.
        daemon.al_pulsar()
        self.assertEqual(grabador.llamadas_iniciar, 1)

        # Llamar a la limpieza directamente.
        daemon._hilo_trabajador = threading.Thread(target=daemon._trabajador, daemon=True)
        daemon._hilo_trabajador.start()
        daemon._detener_limpiamente(ruta_pid=None)

        # detener() debería haberse llamado en la limpieza.
        self.assertEqual(grabador.llamadas_detener, 1)


if __name__ == "__main__":
    unittest.main()
