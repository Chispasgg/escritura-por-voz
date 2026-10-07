"""
Módulo hotkey: captura global de la combinación de teclas configurada.

Diseño:
- parsear_combinacion(): convierte la cadena de config en una estructura
  Combinacion con máscara X11 y keycode. Error explícito si el formato o
  cualquier token son inválidos.

- MaquinaEstados: lógica pura sin X11. Recibe eventos "pulsar"/"soltar"
  (ya filtrada la auto-repetición por la capa X11) y emite los callbacks
  inyectados. Al estar separada se puede testear de forma exhaustiva sin
  servidor X.

- GestorHotkey: capa X11. Abre el display, registra XGrabKey (×4 para las
  combinaciones de LockMask/Mod2Mask) y ejecuta el bucle de eventos.

  Auto-repetición: python-xlib 0.33 no expone XkbSetDetectableAutoRepeat
  (extensión XKB ausente). Se usa la técnica estándar de filtrar pares
  KeyRelease + KeyPress con idéntico timestamp, que es la señal que el
  servidor X emite para indicar auto-repetición.

  Comportamiento al soltar un modificador antes que la tecla base:
  XGrabKey solo entrega eventos para la tecla base (Escape). Si Control
  se suelta antes de Escape, la pulsación continúa hasta que Escape se
  suelta, ya que el grab sigue activo. Este comportamiento es coherente
  con el modelo X11 y con el uso real (el grip se mantiene mientras la
  tecla base esté pulsada).
"""

from __future__ import annotations

import select
import socket
import threading
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable

from Xlib import XK, X
from Xlib import display as xdisplay
from Xlib.error import BadAccess, CatchError

# ---------------------------------------------------------------------------
# Excepciones propias
# ---------------------------------------------------------------------------


class HotkeyError(Exception):
    """Base para errores del módulo hotkey."""


class CombinacionInvalida(HotkeyError):
    """
    La cadena de combinación tiene formato incorrecto o contiene un token
    no reconocido.
    """

    def __init__(self, combinacion: str, motivo: str) -> None:
        self.combinacion = combinacion
        self.motivo = motivo
        super().__init__(f"combinación inválida '{combinacion}': {motivo}")


class GrabFallido(HotkeyError):
    """
    XGrabKey devolvió BadAccess: otra aplicación ya tiene registrada esta
    combinación de teclas.
    """

    def __init__(self, combinacion: str) -> None:
        self.combinacion = combinacion
        super().__init__(
            f"no se pudo registrar '{combinacion}': ya está capturada por otra "
            "aplicación (BadAccess de X11). Cierra la aplicación que tenga "
            "asignada esta combinación de teclas."
        )


# ---------------------------------------------------------------------------
# Parseo de la combinación
# ---------------------------------------------------------------------------

# Nombres de modificador aceptados → máscara X11 correspondiente.
# "ctrl" es alias de "control" para compatibilidad con notaciones habituales.
_MODIFICADORES: dict[str, int] = {
    "shift": X.ShiftMask,
    "control": X.ControlMask,
    "ctrl": X.ControlMask,
    "alt": X.Mod1Mask,
    "mod1": X.Mod1Mask,
    "mod2": X.Mod2Mask,
    "mod3": X.Mod3Mask,
    "mod4": X.Mod4Mask,
    "mod5": X.Mod5Mask,
}


@dataclass(frozen=True)
class Combinacion:
    """Combinación de teclas resuelta: máscara, keysym y keycode."""

    mascara: int  # OR de máscaras de modificador X11
    keysym: int  # keysym X11 de la tecla base
    keycode: int  # keycode del layout activo (depende del teclado)
    cadena: str  # cadena original, para mensajes


def parsear_combinacion(cadena: str, display: xdisplay.Display) -> Combinacion:
    """
    Convierte "Modificador+Modificador+keysym" en una Combinacion resuelta.

    El último token separado por '+' es el keysym; los anteriores son
    modificadores. Sin modificadores (un único token) es válido: la tecla
    base sin modificar.

    Args:
        cadena:  Cadena en formato "Control+Escape", "Mod4+v", etc.
        display: Display X11 abierto (para resolver el keycode del layout activo).

    Raises:
        CombinacionInvalida: Si el formato, un modificador o el keysym son
                              inválidos, o el keysym no tiene keycode asignado.
    """
    cadena_limpia = cadena.strip()
    if not cadena_limpia:
        raise CombinacionInvalida(cadena, "la cadena no puede estar vacía")

    partes = [p.strip() for p in cadena_limpia.split("+")]
    if any(p == "" for p in partes):
        raise CombinacionInvalida(cadena, "hay un token vacío entre separadores '+'")

    # El último token es el keysym; todos los anteriores son modificadores.
    *mods_raw, keysym_raw = partes

    # Construir la máscara combinando los modificadores.
    mascara = 0
    for mod in mods_raw:
        nombre = mod.lower()
        if nombre not in _MODIFICADORES:
            raise CombinacionInvalida(cadena, f"modificador desconocido: '{mod}'")
        mascara |= _MODIFICADORES[nombre]

    # Resolver keysym: string_to_keysym devuelve 0 si no lo reconoce.
    keysym = XK.string_to_keysym(keysym_raw)
    if keysym == 0:
        raise CombinacionInvalida(cadena, f"keysym desconocido: '{keysym_raw}'")

    # Resolver keycode: depende del layout del teclado activo.
    keycode = display.keysym_to_keycode(keysym)
    if keycode == 0:
        raise CombinacionInvalida(
            cadena,
            f"el keysym '{keysym_raw}' (0x{keysym:x}) no tiene keycode en este teclado",
        )

    return Combinacion(mascara=mascara, keysym=keysym, keycode=keycode, cadena=cadena)


# ---------------------------------------------------------------------------
# Máquina de estados pura (sin X11)
# ---------------------------------------------------------------------------


class _Estado(Enum):
    INACTIVO = auto()
    PULSADA = auto()


class MaquinaEstados:
    """
    Máquina de estados pura: sin X11, sin threading.

    Recibe eventos ya desambiguados ("pulsar"/"soltar", sin auto-repetición)
    y emite los callbacks al_pulsar / al_soltar inyectados. Por diseño,
    emite como mucho un al_pulsar y un al_soltar por pulsación física.

    Esta clase es el único lugar donde vive la lógica de decisión; la capa
    X11 (GestorHotkey) se limita a filtrar la auto-repetición antes de llamar
    a procesar().
    """

    def __init__(
        self,
        al_pulsar: Callable[[], None],
        al_soltar: Callable[[], None],
    ) -> None:
        self._al_pulsar = al_pulsar
        self._al_soltar = al_soltar
        self._estado = _Estado.INACTIVO

    def procesar(self, tipo: str) -> None:
        """
        Procesa un evento ya filtrado.

        Args:
            tipo: "pulsar" para una pulsación genuina,
                  "soltar" para un soltado genuino.

        Raises:
            ValueError: Si tipo no es "pulsar" ni "soltar".
        """
        if tipo == "pulsar":
            if self._estado is _Estado.INACTIVO:
                self._estado = _Estado.PULSADA
                self._al_pulsar()
            # PULSADA + "pulsar" → no debería llegar (ya filtrado), se ignora.
        elif tipo == "soltar":
            if self._estado is _Estado.PULSADA:
                self._estado = _Estado.INACTIVO
                self._al_soltar()
            # INACTIVO + "soltar" → se ignora.
        else:
            raise ValueError(f"tipo de evento desconocido: {tipo!r}. Use 'pulsar' o 'soltar'.")

    @property
    def estado(self) -> str:
        """Nombre del estado actual: 'INACTIVO' o 'PULSADA'."""
        return self._estado.name


# ---------------------------------------------------------------------------
# Gestor de hotkey (capa X11)
# ---------------------------------------------------------------------------

# Extras de máscara de bloqueo: se registra la combinación con los cuatro
# valores para que funcione sea cual sea el estado de CapsLock/NumLock.
_MASCARAS_BLOQUEO: tuple[int, ...] = (
    0,
    X.LockMask,  # CapsLock
    X.Mod2Mask,  # NumLock (Mod2 en la mayoría de layouts)
    X.LockMask | X.Mod2Mask,
)


class GestorHotkey:
    """
    Captura global de la tecla mediante XGrabKey.

    owner_events=False en grab_key garantiza que Ctrl+Escape no llegue a la
    aplicación que tenga el foco en ese momento.

    Uso típico (en un hilo dedicado):

        gestor = GestorHotkey("Control+Escape", al_pulsar, al_soltar)
        hilo = threading.Thread(target=gestor.escuchar, daemon=True)
        hilo.start()
        ...
        gestor.detener()
        hilo.join()
    """

    def __init__(
        self,
        combinacion: str,
        al_pulsar: Callable[[], None],
        al_soltar: Callable[[], None],
    ) -> None:
        self._cadena = combinacion
        self._maquina = MaquinaEstados(al_pulsar, al_soltar)
        self._parar = threading.Event()
        # socketpair para despertar el select() desde detener() sin polling puro.
        self._wake_r, self._wake_w = socket.socketpair()

    def escuchar(self) -> None:
        """
        Abre el display X11, registra el grab y entra en el bucle de eventos.
        Bloquea hasta que se llame detener(). Libera el grab, cierra el
        display y cierra los fds del socketpair al salir, tanto en salida
        normal como por excepción.

        Raises:
            CombinacionInvalida: Si la cadena de combinación es inválida.
            GrabFallido:         Si XGrabKey devuelve BadAccess.
        """
        try:
            dpy = xdisplay.Display()
            raiz = dpy.screen().root
            comb = parsear_combinacion(self._cadena, dpy)
            self._registrar_grab(dpy, raiz, comb)
            try:
                self._bucle(dpy, raiz, comb)
            finally:
                self._liberar_grab(dpy, raiz, comb)
                dpy.close()
        finally:
            # Cierra los fds del socketpair en cualquier caso.
            # detener() captura el OSError si se llama después de esto.
            self._wake_r.close()
            self._wake_w.close()

    def detener(self) -> None:
        """
        Señaliza el bucle para que se detenga limpiamente. Thread-safe.
        Seguro llamarlo antes o después de escuchar().
        """
        self._parar.set()
        try:
            self._wake_w.send(b"\x00")
        except OSError:
            pass  # socket ya cerrado o no inicializado

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _registrar_grab(
        self,
        dpy: xdisplay.Display,
        raiz,
        comb: Combinacion,
    ) -> None:
        """
        Registra XGrabKey con las cuatro variantes de máscaras de bloqueo.

        Si alguna variante falla con BadAccess, libera las ya registradas
        y lanza GrabFallido.
        """
        registradas: list[int] = []

        for extra in _MASCARAS_BLOQUEO:
            catcher = CatchError(BadAccess)
            raiz.grab_key(
                comb.keycode,
                comb.mascara | extra,
                False,  # owner_events=False → la tecla no llega a la app con foco
                X.GrabModeAsync,
                X.GrabModeAsync,
                onerror=catcher,
            )
            dpy.sync()

            if catcher.get_error() is not None:
                # Libera las variantes ya registradas antes de propagar el error.
                for extra_reg in registradas:
                    try:
                        raiz.ungrab_key(comb.keycode, comb.mascara | extra_reg)
                    except Exception:
                        pass
                raise GrabFallido(self._cadena)

            registradas.append(extra)

    def _liberar_grab(
        self,
        dpy: xdisplay.Display,
        raiz,
        comb: Combinacion,
    ) -> None:
        for extra in _MASCARAS_BLOQUEO:
            try:
                raiz.ungrab_key(comb.keycode, comb.mascara | extra)
            except Exception:
                pass
        try:
            dpy.flush()
        except Exception:
            pass

    def _bucle(
        self,
        dpy: xdisplay.Display,
        raiz,
        comb: Combinacion,
    ) -> None:
        """
        Bucle de eventos con select() para poder interrumpirlo con detener().
        Timeout de 0.2 s como red de seguridad ante señales.
        """
        fd = dpy.fileno()
        wake_fd = self._wake_r.fileno()

        while not self._parar.is_set():
            readable, _, _ = select.select([fd, wake_fd], [], [], 0.2)

            if wake_fd in readable:
                break

            if fd not in readable:
                continue

            while dpy.pending_events():
                ev = dpy.next_event()
                self._procesar_evento(dpy, ev, comb)

    def _procesar_evento(
        self,
        dpy: xdisplay.Display,
        ev,
        comb: Combinacion,
    ) -> None:
        """
        Filtra auto-repetición y alimenta la máquina de estados.

        Técnica de filtrado sin XKB: el servidor X genera un par
        KeyRelease + KeyPress con timestamp idéntico para cada ciclo de
        auto-repetición. Si el siguiente evento pendiente tiene ese patrón,
        descartamos ambos sin emitir ningún evento a la máquina de estados.

        Nota sobre pending_events() y la «carrera»:
          pending_events() cuenta los eventos ya presentes en el buffer local
          de python-xlib; no realiza ninguna llamada bloqueante al socket.
          Existe una carrera teórica entre pending_events() y next_event():
          si otro hilo leyera el mismo display, el buffer podría vaciarse
          entre las dos llamadas. En este módulo hay un único hilo consumidor
          por instancia de Display, así que la carrera no puede ocurrir.
          En un display local (socket Unix) la entrega es síncrona, por lo
          que pending_events() == 1 implica que next_event() devuelve
          inmediatamente sin bloquear.
        """
        if ev.type == X.KeyPress and ev.detail == comb.keycode:
            self._maquina.procesar("pulsar")

        elif ev.type == X.KeyRelease and ev.detail == comb.keycode:
            if dpy.pending_events():
                # pending_events() es no bloqueante: solo cuenta el buffer local.
                siguiente = dpy.next_event()
                if siguiente.type == X.KeyPress and siguiente.detail == ev.detail and siguiente.time == ev.time:
                    # Par KeyRelease+KeyPress con mismo timestamp → auto-repetición.
                    # Descartamos ambos sin notificar a la máquina de estados.
                    return
                # El siguiente evento no forma par de auto-rep: el release es genuino.
                self._maquina.procesar("soltar")
                # Procesamos el evento que ya sacamos del buffer.
                self._procesar_evento(dpy, siguiente, comb)
            else:
                # No hay evento siguiente pendiente → release genuino.
                self._maquina.procesar("soltar")
