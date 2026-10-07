"""
Tests del módulo app.whisper_ptt.hotkey.

Cubre tres áreas sin X11:
  1. parsear_combinacion() — formato válido e inválido, usando un doble de
     Display que resuelve keycodes ficticios.
  2. MaquinaEstados — todas las transiciones, autorrepetición y tipo inválido.
  3. GestorHotkey._procesar_evento() — llamada al código real con un doble de
     Display que sirve eventos desde una cola controlada (pending_events /
     next_event), verificando filtrado de auto-repetición, releases genuinos
     y comportamiento ante keycodes ajenos.
"""

from __future__ import annotations

import types
import unittest

from Xlib import X

from app.whisper_ptt.hotkey import (
    Combinacion,
    CombinacionInvalida,
    GestorHotkey,
    MaquinaEstados,
    parsear_combinacion,
)

# ---------------------------------------------------------------------------
# Doble de Display para parsear_combinacion sin X11
# ---------------------------------------------------------------------------


class _DisplayDoble:
    """
    Doble mínimo del Display X11.

    keysym_to_keycode() devuelve un keycode ficticio para cualquier keysym
    válido (≠ 0), simulando un layout que tiene todas las teclas.
    Para el keysym 0xDEAD devuelve 0, simulando una tecla sin keycode.
    """

    def keysym_to_keycode(self, keysym: int) -> int:
        if keysym == 0xDEAD:
            return 0
        return (keysym % 100) + 10  # valor siempre > 0


# ---------------------------------------------------------------------------
# Tests de parsear_combinacion
# ---------------------------------------------------------------------------


class TestParsearCombinacion(unittest.TestCase):
    def setUp(self) -> None:
        self.dpy = _DisplayDoble()

    # --- Casos válidos ---

    def test_control_escape(self) -> None:
        from Xlib import XK, X

        comb = parsear_combinacion("Control+Escape", self.dpy)
        self.assertEqual(comb.mascara, X.ControlMask)
        self.assertEqual(comb.keysym, XK.string_to_keysym("Escape"))
        self.assertGreater(comb.keycode, 0)
        self.assertEqual(comb.cadena, "Control+Escape")

    def test_shift_f1(self) -> None:
        from Xlib import X

        comb = parsear_combinacion("Shift+F1", self.dpy)
        self.assertEqual(comb.mascara, X.ShiftMask)

    def test_control_alt_delete(self) -> None:
        from Xlib import X

        comb = parsear_combinacion("Control+Alt+Delete", self.dpy)
        self.assertEqual(comb.mascara, X.ControlMask | X.Mod1Mask)

    def test_ctrl_es_alias_de_control(self) -> None:
        from Xlib import X

        comb = parsear_combinacion("Ctrl+Escape", self.dpy)
        self.assertEqual(comb.mascara, X.ControlMask)

    def test_solo_keysym_sin_modificadores(self) -> None:
        """Una sola tecla base sin modificadores es válida (máscara cero)."""
        comb = parsear_combinacion("F12", self.dpy)
        self.assertEqual(comb.mascara, 0)
        self.assertGreater(comb.keycode, 0)

    def test_mod4_v(self) -> None:
        from Xlib import X

        comb = parsear_combinacion("Mod4+v", self.dpy)
        self.assertEqual(comb.mascara, X.Mod4Mask)

    def test_modificadores_acumulan_mascara(self) -> None:
        from Xlib import X

        comb = parsear_combinacion("Shift+Control+Alt+Escape", self.dpy)
        self.assertEqual(comb.mascara, X.ShiftMask | X.ControlMask | X.Mod1Mask)

    def test_cadena_guarda_original(self) -> None:
        comb = parsear_combinacion("Control+Escape", self.dpy)
        self.assertEqual(comb.cadena, "Control+Escape")

    def test_espacios_en_blanco_ignorados(self) -> None:
        """Espacios alrededor de los tokens se eliminan."""
        from Xlib import X

        comb = parsear_combinacion("  Control + Escape  ", self.dpy)
        self.assertEqual(comb.mascara, X.ControlMask)

    # --- Casos inválidos ---

    def test_modificador_desconocido_lanza_error(self) -> None:
        with self.assertRaises(CombinacionInvalida) as ctx:
            parsear_combinacion("Super+Escape", self.dpy)
        self.assertIn("Super", ctx.exception.motivo)

    def test_keysym_desconocido_lanza_error(self) -> None:
        with self.assertRaises(CombinacionInvalida) as ctx:
            parsear_combinacion("Control+NoExiste", self.dpy)
        self.assertIn("NoExiste", ctx.exception.motivo)

    def test_cadena_vacia_lanza_error(self) -> None:
        with self.assertRaises(CombinacionInvalida):
            parsear_combinacion("", self.dpy)

    def test_cadena_solo_espacios_lanza_error(self) -> None:
        with self.assertRaises(CombinacionInvalida):
            parsear_combinacion("   ", self.dpy)

    def test_token_vacio_entre_separadores_lanza_error(self) -> None:
        with self.assertRaises(CombinacionInvalida):
            parsear_combinacion("Control++Escape", self.dpy)

    def test_combinacion_empieza_con_mas_lanza_error(self) -> None:
        with self.assertRaises(CombinacionInvalida):
            parsear_combinacion("+Escape", self.dpy)

    def test_excepcion_lleva_combinacion_original(self) -> None:
        cadena = "Control+NoExiste"
        with self.assertRaises(CombinacionInvalida) as ctx:
            parsear_combinacion(cadena, self.dpy)
        self.assertEqual(ctx.exception.combinacion, cadena)


# ---------------------------------------------------------------------------
# Tests de MaquinaEstados
# ---------------------------------------------------------------------------


class TestMaquinaEstados(unittest.TestCase):
    def _nueva(self) -> tuple[MaquinaEstados, list, list]:
        pulsaciones: list[int] = []
        soltadas: list[int] = []
        maquina = MaquinaEstados(
            al_pulsar=lambda: pulsaciones.append(1),
            al_soltar=lambda: soltadas.append(1),
        )
        return maquina, pulsaciones, soltadas

    # --- Estado inicial ---

    def test_estado_inicial_es_inactivo(self) -> None:
        maquina, _, _ = self._nueva()
        self.assertEqual(maquina.estado, "INACTIVO")

    # --- Flujo normal ---

    def test_pulsar_cambia_estado_a_pulsada(self) -> None:
        maquina, _, _ = self._nueva()
        maquina.procesar("pulsar")
        self.assertEqual(maquina.estado, "PULSADA")

    def test_pulsar_llama_callback_al_pulsar(self) -> None:
        maquina, pulsaciones, _ = self._nueva()
        maquina.procesar("pulsar")
        self.assertEqual(len(pulsaciones), 1)

    def test_soltar_tras_pulsar_llama_callback_al_soltar(self) -> None:
        maquina, _, soltadas = self._nueva()
        maquina.procesar("pulsar")
        maquina.procesar("soltar")
        self.assertEqual(len(soltadas), 1)

    def test_soltar_tras_pulsar_vuelve_a_inactivo(self) -> None:
        maquina, _, _ = self._nueva()
        maquina.procesar("pulsar")
        maquina.procesar("soltar")
        self.assertEqual(maquina.estado, "INACTIVO")

    def test_ciclo_completo_dos_veces_genera_dos_pares(self) -> None:
        maquina, pulsaciones, soltadas = self._nueva()
        for _ in range(2):
            maquina.procesar("pulsar")
            maquina.procesar("soltar")
        self.assertEqual(len(pulsaciones), 2)
        self.assertEqual(len(soltadas), 2)

    # --- Auto-repetición: la capa X11 filtra, pero la máquina es robusta ---

    def test_segundo_pulsar_mientras_pulsada_se_ignora(self) -> None:
        """Si llegara un 'pulsar' repetido, la máquina no genera un segundo evento."""
        maquina, pulsaciones, _ = self._nueva()
        maquina.procesar("pulsar")
        maquina.procesar("pulsar")
        maquina.procesar("pulsar")
        self.assertEqual(len(pulsaciones), 1)
        self.assertEqual(maquina.estado, "PULSADA")

    def test_segundo_soltar_desde_inactivo_se_ignora(self) -> None:
        maquina, _, soltadas = self._nueva()
        maquina.procesar("pulsar")
        maquina.procesar("soltar")
        maquina.procesar("soltar")  # doble soltada
        self.assertEqual(len(soltadas), 1)
        self.assertEqual(maquina.estado, "INACTIVO")

    # --- Soltar modificador antes que la tecla base ---

    def test_soltar_desde_inactivo_no_dispara_callback(self) -> None:
        """
        Si Control se suelta antes que Escape, la máquina (que solo recibe
        eventos para la tecla base) permanece en INACTIVO.
        """
        maquina, _, soltadas = self._nueva()
        maquina.procesar("soltar")
        self.assertEqual(len(soltadas), 0)
        self.assertEqual(maquina.estado, "INACTIVO")

    def test_soltar_ctrl_antes_escape_pulsacion_continua(self) -> None:
        """
        Secuencia: pulsar (combo activo), luego soltar (solo Escape llega al
        grab). La pulsación dura hasta el soltar.
        """
        maquina, pulsaciones, soltadas = self._nueva()
        maquina.procesar("pulsar")
        # Ctrl suelto → el grab no lo entrega; la máquina no recibe nada.
        # Escape suelto → el grab entrega el KeyRelease.
        maquina.procesar("soltar")
        self.assertEqual(len(pulsaciones), 1)
        self.assertEqual(len(soltadas), 1)

    # --- Tipo inválido ---

    def test_tipo_invalido_lanza_valueerror(self) -> None:
        maquina, _, _ = self._nueva()
        with self.assertRaises(ValueError) as ctx:
            maquina.procesar("clickar")
        self.assertIn("clickar", str(ctx.exception))

    # --- Callback se llama exactamente una vez por transición ---

    def test_callbacks_exactamente_una_vez_por_transicion(self) -> None:
        maquina, pulsaciones, soltadas = self._nueva()
        maquina.procesar("soltar")  # INACTIVO → ignorado
        maquina.procesar("pulsar")  # INACTIVO → PULSADA (al_pulsar ×1)
        maquina.procesar("pulsar")  # PULSADA → ignorado
        maquina.procesar("soltar")  # PULSADA → INACTIVO (al_soltar ×1)
        maquina.procesar("soltar")  # INACTIVO → ignorado
        self.assertEqual(len(pulsaciones), 1)
        self.assertEqual(len(soltadas), 1)


# ---------------------------------------------------------------------------
# Infraestructura de dobles para TestProcesarEvento
# ---------------------------------------------------------------------------

# Keycode ficticio para la tecla grabada en todos los tests de _procesar_evento.
_KC = 9

# Combinacion fija: no necesita display real porque solo se usa el campo keycode.
_COMB = Combinacion(mascara=X.ControlMask, keysym=65307, keycode=_KC, cadena="Control+Escape")


def _ev(tipo: int, detail: int, time: int) -> types.SimpleNamespace:
    """Objeto evento mínimo con los atributos que lee _procesar_evento."""
    return types.SimpleNamespace(type=tipo, detail=detail, time=time)


class _DisplayCola:
    """
    Doble de Display que sirve una cola predefinida de eventos.

    pending_events() devuelve el número de eventos en la cola (no bloquea).
    next_event() extrae y devuelve el primero.
    No abre ninguna conexión X11.
    """

    def __init__(self, eventos: list | None = None) -> None:
        self._cola: list = list(eventos or [])

    def pending_events(self) -> int:
        return len(self._cola)

    def next_event(self) -> types.SimpleNamespace:
        return self._cola.pop(0)


# ---------------------------------------------------------------------------
# Tests de GestorHotkey._procesar_evento (código real, display simulado)
# ---------------------------------------------------------------------------


class TestProcesarEvento(unittest.TestCase):
    """
    Verifica _procesar_evento() llamando al código real de GestorHotkey.

    Usa _DisplayCola para controlar exactamente qué devuelven
    pending_events() y next_event(), y objetos SimpleNamespace como eventos.
    Los callbacks espía detectan qué emite la MaquinaEstados real.
    """

    def setUp(self) -> None:
        self.pulsaciones: list[int] = []
        self.soltadas: list[int] = []
        self.gestor = GestorHotkey(
            "Control+Escape",
            al_pulsar=lambda: self.pulsaciones.append(1),
            al_soltar=lambda: self.soltadas.append(1),
        )

    def tearDown(self) -> None:
        # El socketpair se cierra en escuchar(); como no la llamamos en estos
        # tests, lo cerramos aquí para no dejar fds abiertos.
        for s in (self.gestor._wake_r, self.gestor._wake_w):
            try:
                s.close()
            except OSError:
                pass

    def _pulsar(self) -> None:
        """
        Pone la máquina en estado PULSADA pasando por el código real
        (_procesar_evento con un KeyPress) y resetea los contadores espía.

        Se usa el camino real en lugar de tocar _maquina directamente para
        que _pulsar() ejercite la misma ruta de código que los tests comprueban,
        evitando que pase aunque la lógica de _procesar_evento cambie.
        El callback al_pulsar se descarta limpiando las listas; la aserción
        posterior mide solo lo que ocurre en el evento bajo prueba.
        """
        dpy = _DisplayCola()
        self.gestor._procesar_evento(dpy, _ev(X.KeyPress, _KC, 100), _COMB)
        self.pulsaciones.clear()
        self.soltadas.clear()

    # --- Pulsación normal ---

    def test_keypress_genera_al_pulsar(self) -> None:
        dpy = _DisplayCola()
        self.gestor._procesar_evento(dpy, _ev(X.KeyPress, _KC, 100), _COMB)
        self.assertEqual(len(self.pulsaciones), 1)
        self.assertEqual(len(self.soltadas), 0)

    def test_keyrelease_sin_siguiente_genera_al_soltar(self) -> None:
        """KeyRelease sin evento siguiente → release genuino → al_soltar."""
        self._pulsar()
        dpy = _DisplayCola()  # cola vacía: pending_events() == 0
        self.gestor._procesar_evento(dpy, _ev(X.KeyRelease, _KC, 200), _COMB)
        self.assertEqual(len(self.soltadas), 1)
        self.assertEqual(len(self.pulsaciones), 0)

    # --- Auto-repetición ---

    def test_autorepeticion_release_press_mismo_ts_descartados(self) -> None:
        """
        Par KeyRelease+KeyPress con timestamp idéntico → auto-repetición:
        ninguno de los dos genera callback.
        """
        self._pulsar()
        sig = _ev(X.KeyPress, _KC, 150)  # mismo ts que el release → auto-rep
        dpy = _DisplayCola([sig])
        self.gestor._procesar_evento(dpy, _ev(X.KeyRelease, _KC, 150), _COMB)
        self.assertEqual(len(self.soltadas), 0)
        self.assertEqual(len(self.pulsaciones), 0)

    def test_autorepeticion_cola_queda_vacia_tras_descartar(self) -> None:
        """La cola del display queda vacía tras descartar el par auto-rep."""
        self._pulsar()
        sig = _ev(X.KeyPress, _KC, 150)
        dpy = _DisplayCola([sig])
        self.gestor._procesar_evento(dpy, _ev(X.KeyRelease, _KC, 150), _COMB)
        self.assertEqual(dpy.pending_events(), 0)

    # --- Release genuino seguido de un press (ts distinto) ---

    def test_release_seguido_de_press_ts_distinto_dos_transiciones(self) -> None:
        """
        KeyRelease con siguiente KeyPress de timestamp distinto: el release
        es genuino y el press se procesa recursivamente (segunda pulsación).
        """
        self._pulsar()  # primera pulsación activa
        sig = _ev(X.KeyPress, _KC, 300)  # ts 300 ≠ 200 → no es auto-rep
        dpy = _DisplayCola([sig])
        self.gestor._procesar_evento(dpy, _ev(X.KeyRelease, _KC, 200), _COMB)
        # Release genuino → al_soltar; press siguiente → al_pulsar
        self.assertEqual(len(self.soltadas), 1)
        self.assertEqual(len(self.pulsaciones), 1)

    # --- Keycodes ajenos ---

    def test_keypress_otro_keycode_se_ignora(self) -> None:
        dpy = _DisplayCola()
        self.gestor._procesar_evento(dpy, _ev(X.KeyPress, _KC + 1, 100), _COMB)
        self.assertEqual(len(self.pulsaciones), 0)

    def test_keyrelease_otro_keycode_se_ignora(self) -> None:
        self._pulsar()
        dpy = _DisplayCola()
        self.gestor._procesar_evento(dpy, _ev(X.KeyRelease, _KC + 1, 200), _COMB)
        self.assertEqual(len(self.soltadas), 0)


if __name__ == "__main__":
    unittest.main()
