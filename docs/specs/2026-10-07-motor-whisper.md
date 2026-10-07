# Spec: motor Whisper push-to-talk para escritura-por-voz

## Objetivo
Sustituir VOSK por faster-whisper como motor por defecto, con dictado push-to-talk: se mantiene pulsado **Ctrl+Escape**, se habla, se suelta, y el texto aparece ya puntuado donde esté el foco. VOSK sigue disponible como motor alternativo, sin cambios en su comportamiento.

## Entorno verificado
GNOME Classic sobre X11 · RTX 4060 8 GB, driver 580 (CUDA 13) · cuDNN 9 en el sistema · PipeWire (`pw-record`) · `xclip` + `xdotool` · Python 3.12 en `.venv/`. Ctrl+Escape no tiene atajo asignado en GNOME.

## Comportamiento
1. `lanzar.sh` arranca el motor configurado. Con Whisper, un daemon residente carga el modelo **una sola vez** y espera.
2. Pulsar Ctrl+Escape → empieza a grabar (16 kHz mono, `pw-record`).
3. Soltar → para, transcribe (`language="es"`, filtro VAD) y pega el texto, seguido de un espacio: lo copia con `xclip` a las selecciones de `[salida] selecciones` (por defecto CLIPBOARD y PRIMARY) y pulsa `[salida] tecla_pegar` (por defecto Shift+Insert) con `xdotool`. Así funciona en terminales como WezTerm o xterm, que leen PRIMARY, y en GTK y Firefox, que leen CLIPBOARD (cambio de T-009).
4. Grabaciones más cortas que un mínimo configurable o transcripciones vacías se ignoran.
5. `parar.sh` detiene el motor que esté en marcha. Una sola instancia a la vez (fichero PID).

## Decisiones de diseño
- **Captura de la tecla:** `XGrabKey` vía `python-xlib`. Captura la combinación en exclusiva, así que **el Escape no llega a la aplicación con foco**; si llegara, interrumpiría a Claude Code. La autorrepetición del teclado se neutraliza descartando los pares KeyRelease+KeyPress con el mismo timestamp (python-xlib 0.33 no expone XKB detectable auto-repeat), para que mantener pulsada la tecla no genere pulsaciones y sueltas falsas. También se registra con Bloq Mayús y Bloq Num activos.
- **Modelo:** `large-v3-turbo`, `device=cuda`, `compute_type=float16`, con alternativa configurable a CPU `int8`. Un `initial_prompt` opcional con vocabulario técnico mejora la jerga.
- **Configuración:** `config/config.ini` (stdlib `configparser`, sin dependencia YAML): motor activo, tecla, modelo, dispositivo, idioma, prompt inicial, duración mínima y ruta del modelo VOSK. `lanzar.sh` y el daemon leen solo de ahí.
- **Dependencias nuevas:** `faster-whisper` (motor) y `python-xlib` (tecla global). No hay alternativa en la stdlib para ninguna de las dos.
- **Módulos (`app/whisper_ptt/`)**, cada uno con una responsabilidad e inyectado en el daemon:
  - `config`: carga y valida.
  - `hotkey`: grab de X11 y máquina de estados pulsar/soltar.
  - `grabador`: `pw-record` a memoria.
  - `transcriptor`: faster-whisper, detrás de una interfaz para poder sustituirlo en los tests.
  - `salida`: portapapeles + Ctrl+V.
  - `daemon`: orquesta los anteriores.
- **VOSK:** `app/nerd-dictation` y su config quedan intactos; solo cambia cómo los invoca `lanzar.sh`.

## Fuera de alcance
TTS de las respuestas (hook de Claude Code), envío automático con Enter, Wayland, comandos de puntuación hablados.

## Criterios de aceptación
- Con el motor Whisper: mantener Ctrl+Escape, decir «hola, ¿qué tal estás? Esto es una prueba.», soltar → aparece el texto con comas, signos y mayúsculas en el editor con foco. Escape no llega a la aplicación.
- Con `motor = vosk`, `lanzar.sh` se comporta exactamente como hoy.
- Tests unitarios sin GPU ni X11 (dobles para transcriptor, grabador y salida) en verde; ruff limpio.
