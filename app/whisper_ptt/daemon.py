"""
Daemon de dictado push-to-talk: orquesta grabador, transcriptor,
salida y hotkey.

Diseño:
- El daemon no construye ninguna dependencia; todas se inyectan.
  __main__.py construye las piezas y las pasa.
- al_pulsar / al_soltar se exponen como métodos públicos para que
  __main__.py los pase al GestorHotkey al construirlo.
- La transcripción y el pegado se ejecutan en un único hilo trabajador
  (cola FIFO) para que el bucle de eventos X11 del hilo principal nunca
  espere a la GPU. Las pulsaciones durante una transcripción se encolan
  en orden.
- Los errores de grabador / transcriptor / salida se registran y el
  daemon sigue vivo. Solo los errores de arranque (config inválida,
  modelo que no carga, grab fallido) terminan el proceso.
- Instancia única mediante fichero PID creado con O_EXCL; si hay un PID
  obsoleto (proceso muerto), se reemplaza.
- SIGTERM y SIGINT provocan una parada limpia desde el hilo principal.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from app.whisper_ptt.grabador import GrabadorError, GrabadorProtocol
from app.whisper_ptt.hotkey import GestorHotkey
from app.whisper_ptt.salida import Salida, SalidaError
from app.whisper_ptt.transcriptor import TranscriptorError, TranscriptorProtocol

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Excepción de instancia duplicada
# ---------------------------------------------------------------------------


class DaemonYaEnMarcha(Exception):
    """Ya hay una instancia del daemon en marcha con ese fichero PID."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        super().__init__(
            f"Ya hay una instancia del daemon en marcha (PID {pid}). "
            "Detén la instancia anterior antes de arrancar una nueva. "
            "Si el proceso ya no existe, borra el fichero PID manualmente."
        )


# ---------------------------------------------------------------------------
# Gestión del fichero PID
# ---------------------------------------------------------------------------


def _crear_pid(ruta: Path) -> None:
    """
    Crea el fichero PID de forma segura (O_EXCL) en la ruta indicada.

    Si el fichero existe con un PID de proceso muerto (obsoleto), lo
    reemplaza. Si el proceso está vivo, lanza DaemonYaEnMarcha.

    El directorio padre se crea si no existe.

    Raises:
        DaemonYaEnMarcha: Si ya hay una instancia viva con ese PID.
        OSError: Si no se puede crear el directorio o el fichero.
    """
    ruta.parent.mkdir(parents=True, exist_ok=True)
    pid_propio = str(os.getpid()).encode()

    while True:
        try:
            # O_EXCL garantiza creación atómica: falla si el fichero ya existe.
            fd = os.open(str(ruta), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, pid_propio)
            finally:
                os.close(fd)
            return  # Fichero creado; somos la única instancia.

        except FileExistsError:
            # El fichero existe: averiguar si el PID que contiene está vivo.
            try:
                contenido = ruta.read_text(encoding="ascii", errors="replace").strip()
                pid_otro = int(contenido)
            except (OSError, ValueError):
                # Fichero ilegible o sin número: PID corrupto → borrar y reintentar.
                try:
                    ruta.unlink()
                except FileNotFoundError:
                    pass
                continue

            try:
                # kill(pid, 0) comprueba existencia sin enviar señal real.
                os.kill(pid_otro, 0)
            except ProcessLookupError:
                # Proceso inexistente: PID obsoleto → borrar y reintentar.
                try:
                    ruta.unlink()
                except FileNotFoundError:
                    pass
                continue
            except PermissionError:
                # Proceso existe pero no tenemos permiso para señalizarlo:
                # hay otra instancia en marcha (p.ej. de otro usuario).
                raise DaemonYaEnMarcha(pid_otro) from None

            raise DaemonYaEnMarcha(pid_otro)


def _borrar_pid(ruta: Path) -> None:
    """
    Borra el fichero PID solo si contiene nuestro PID.

    La comprobación evita borrar un fichero ajeno si arrancó otra instancia
    mientras nosotros estábamos cerrando (escenario improbable pero posible).
    """
    try:
        contenido = ruta.read_text(encoding="ascii", errors="replace").strip()
        if contenido == str(os.getpid()):
            ruta.unlink()
    except (OSError, ValueError):
        # Fichero ya borrado o ilegible: no es un error.
        pass


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class Daemon:
    """
    Orquesta grabador, transcriptor, salida y hotkey.

    Las dependencias se inyectan desde __main__.py; el daemon no construye
    nada. Expone al_pulsar y al_soltar para que __main__.py los pase al
    GestorHotkey al construirlo.

    Uso típico en __main__.py::

        daemon = Daemon(grabador, transcriptor, salida)
        gestor = GestorHotkey(combinacion, daemon.al_pulsar, daemon.al_soltar)
        daemon.arrancar(gestor, ruta_pid=Path(config.general.fichero_pid))
    """

    def __init__(
        self,
        grabador: GrabadorProtocol,
        transcriptor: TranscriptorProtocol,
        salida: Salida,
    ) -> None:
        self._grabador = grabador
        self._transcriptor = transcriptor
        self._salida = salida

        # Cola FIFO: contiene arrays de audio pendientes de transcripción.
        # El centinela None señaliza al hilo trabajador que debe terminar.
        self._cola: queue.Queue[Optional[np.ndarray]] = queue.Queue()

        # _stop indica al trabajador que vacíe la cola y termine.
        self._stop = threading.Event()

        # Referencia al hilo trabajador (asignada en arrancar).
        self._hilo_trabajador: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Callbacks públicos para GestorHotkey
    # ------------------------------------------------------------------

    def al_pulsar(self) -> None:
        """
        Arranca la grabación.

        Llamado desde el bucle de eventos X11 (hilo principal). Si el
        grabador ya estaba activo por una pulsación incoherente, se
        registra el error y se continúa sin romper nada.
        """
        try:
            self._grabador.iniciar()
            log.debug("Grabación iniciada.")
        except GrabadorError as exc:
            log.error("No se pudo iniciar la grabación: %s", exc)

    def al_soltar(self) -> None:
        """
        Detiene la grabación y encola el audio para el hilo trabajador.

        Si el audio es demasiado corto (grabador devuelve array vacío),
        se descarta sin transcribir. Si no había grabación activa (suelta
        incoherente), se registra y se ignora.
        """
        try:
            audio = self._grabador.detener()
        except GrabadorError as exc:
            log.error("Error al detener la grabación: %s", exc)
            return

        if len(audio) == 0:
            log.debug("Grabación descartada (demasiado corta o silencio).")
            return

        self._cola.put(audio)
        log.debug("Audio encolado (%d muestras).", len(audio))

    # ------------------------------------------------------------------
    # Hilo trabajador
    # ------------------------------------------------------------------

    def _trabajador(self) -> None:
        """
        Consume la cola FIFO: transcribe cada audio y pega el resultado.

        Errores de transcriptor o salida se registran sin interrumpir el
        bucle. Termina cuando recibe el centinela None o cuando _stop está
        activo y la cola está vacía.
        """
        while True:
            try:
                audio = self._cola.get(timeout=0.2)
            except queue.Empty:
                # Timeout periódico: comprobar si hay que salir.
                if self._stop.is_set():
                    break
                continue

            if audio is None:
                # Centinela de parada: salir del bucle inmediatamente.
                break

            # Transcribir
            try:
                texto = self._transcriptor.transcribir(audio)
            except TranscriptorError as exc:
                log.error("Error en la transcripción: %s", exc)
                continue

            if not texto:
                log.debug("Transcripción vacía: nada que pegar.")
                continue

            # Pegar
            try:
                self._salida.escribir(texto)
                log.debug("Texto pegado: %r", texto)
            except SalidaError as exc:
                log.error("Error al pegar el texto: %s", exc)

    # ------------------------------------------------------------------
    # Arranque y parada
    # ------------------------------------------------------------------

    def arrancar(
        self,
        gestor: GestorHotkey,
        ruta_pid: Path | None = None,
    ) -> None:
        """
        Arranca el daemon.

        Orden de operaciones:
          1. Crea el fichero PID (instancia única).
          2. Lanza el hilo trabajador.
          3. Registra manejadores SIGTERM/SIGINT.
          4. Bloquea en el bucle X11 de *gestor* (hilo principal).
          5. Al retornar (detener() o señal), limpia todo.

        Args:
            gestor:    GestorHotkey ya construido con al_pulsar/al_soltar
                       de esta instancia.
            ruta_pid:  Ruta del fichero PID. Si es None no se gestiona
                       instancia única (útil en tests).

        Raises:
            DaemonYaEnMarcha: Si ya hay una instancia viva.
            CombinacionInvalida, GrabFallido: propagados por gestor.escuchar().
        """
        # Instancia única
        if ruta_pid is not None:
            _crear_pid(ruta_pid)

        # Hilo trabajador
        self._hilo_trabajador = threading.Thread(
            target=self._trabajador,
            daemon=True,
            name="whisper-ptt-trabajador",
        )
        self._hilo_trabajador.start()

        # Manejadores de señal: se ejecutan en el hilo principal.
        # Llaman a gestor.detener() para que escuchar() retorne.
        def _manejador(signum: int, _frame) -> None:
            nombre = signal.Signals(signum).name
            log.info("Señal %s recibida: iniciando parada limpia.", nombre)
            gestor.detener()

        try:
            signal.signal(signal.SIGTERM, _manejador)
            signal.signal(signal.SIGINT, _manejador)
        except ValueError:
            # signal.signal() solo funciona en el hilo principal de Python.
            # En producción __main__.py llama a arrancar() siempre desde el
            # hilo principal; en tests puede usarse desde hilos auxiliares,
            # donde la gestión de señales no es necesaria.
            log.debug("No se registraron señales (hilo no principal).")

        try:
            # Bucle bloqueante: retorna cuando gestor.detener() es llamado.
            gestor.escuchar()
        finally:
            self._detener_limpiamente(ruta_pid)

    def _detener_limpiamente(self, ruta_pid: Path | None) -> None:
        """
        Limpieza post-bucle:
          - Para el grabador si estaba grabando.
          - Descarta la cola pendiente.
          - Envía el centinela al trabajador y espera que termine.
          - Borra el fichero PID (solo si es el nuestro).
        """
        # Parar grabador por si estaba grabando al recibir la señal.
        try:
            self._grabador.detener()
        except GrabadorError:
            pass  # No estaba grabando: es el caso normal.

        # Descartar trabajos pendientes: enviamos el centinela directamente.
        # No tiene sentido transcribir audio tras recibir SIGTERM.
        while not self._cola.empty():
            try:
                self._cola.get_nowait()
            except queue.Empty:
                break
        self._cola.put(None)  # Centinela de parada.

        # Señalizar al trabajador (cubre el path del timeout del Empty).
        self._stop.set()

        # Esperar al hilo trabajador.
        if self._hilo_trabajador is not None:
            self._hilo_trabajador.join(timeout=5.0)

        # Borrar el fichero PID solo si contiene nuestro PID.
        if ruta_pid is not None:
            _borrar_pid(ruta_pid)

        log.info("Daemon detenido.")
