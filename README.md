# Escritura por voz

Dictado de voz offline para Linux en español. Reconoce lo que dices y lo escribe directamente donde tengas el foco, sin servicios en la nube.

Soporta dos motores: **Whisper** (GPU, push-to-talk) y **VOSK** (CPU, continuo).

## Tecnologías

| Tecnología | Rol |
|---|---|
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Motor Whisper optimizado (GPU/CPU), modelo `large-v3-turbo` |
| [python-xlib](https://github.com/python-xlib/python-xlib) | Captura de tecla global (`XGrabKey`) en X11 |
| [VOSK](https://alphacephei.com/vosk/) | Motor de transcripción alternativo (CPU, sin conexión) |
| Modelo `vosk-model-es-0.42` | Modelo grande de español a 16 kHz para VOSK |
| [PipeWire](https://pipewire.org/) | Captura de audio del micrófono (`pw-record`) |
| [xdotool](https://github.com/jordansissel/xdotool) | Simulación de teclado para pegar el texto en X11 |
| [xclip](https://github.com/astrand/xclip) | Portapapeles — permite tildes y caracteres especiales |
| Python 3.12 | Lenguaje del daemon y scripts de control |

## Cómo funciona

### Motor Whisper (por defecto)

1. `lanzar.sh` arranca un daemon en segundo plano que carga el modelo una sola vez (~3,5 s con caché; ~145 s la primera vez, mientras descarga).
2. Mantén pulsado **Ctrl+Escape** → el micrófono empieza a grabar (PipeWire, 16 kHz mono).
3. Suelta → el audio se transcribe localmente con faster-whisper (GPU, filtro VAD) y el texto aparece donde esté el foco.
4. `parar.sh` envía SIGTERM al daemon y espera a que termine limpiamente.

El texto se copia al portapapeles y a la selección primaria con `xclip`, y se pega con **Shift+Insert** (configurable en `config/config.ini`). Shift+Insert funciona en terminales como WezTerm y xterm, en aplicaciones GTK y en Firefox; no depende del tipo de ventana con foco. Puedes ajustar `tecla_pegar` y `selecciones` en la sección `[salida]` de `config/config.ini`.

### Motor VOSK (alternativo)

1. `lanzar.sh` arranca `nerd-dictation` en segundo plano con el modelo VOSK local.
2. Habla: la transcripción es continua mientras el proceso esté en marcha.
3. `parar.sh` llama a `nerd-dictation end`.

## Uso

```sh
./lanzar.sh   # iniciar dictado
./parar.sh    # detener dictado
```

Para cambiar de motor, edita `config/config.ini`:

```ini
[general]
motor = whisper   # o: motor = vosk
```

## Requisitos

```sh
# Sistema
sudo apt install xdotool xclip python3 python3-venv

# PipeWire (normalmente ya instalado en distros modernas)
sudo apt install pipewire
```

Para el motor **Whisper** necesitas además:
- Driver NVIDIA ≥ 520 con CUDA 11.8+ y cuDNN 8+
- La primera ejecución descarga automáticamente el modelo `large-v3-turbo` (~1,6 GB desde HuggingFace). Los arranques posteriores son inmediatos (caché local de faster-whisper).

Para usar Whisper en **CPU** (sin GPU NVIDIA), edita `config/config.ini`:

```ini
[whisper]
dispositivo = cpu
tipo_computo = int8
```

## Ejecutar tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Los tests unitarios no requieren GPU ni X11: usan dobles para el transcriptor, el grabador y la salida. Los tests de integración que tocan el portapapeles o el micrófono solo se ejecutan cuando se activan explícitamente:

```sh
ESCRITURA_TESTS_INTEGRACION=1 .venv/bin/python -m unittest discover -s tests -v
```

## Estructura

```
app/
  nerd-dictation          # script de dictado continuo (VOSK)
  config/
    nerd-dictation.py     # configuración de nerd-dictation
  whisper_ptt/            # daemon push-to-talk (Whisper)
    __main__.py           # punto de entrada: python -m app.whisper_ptt
    config.py             # carga y validación de config.ini
    daemon.py             # orquestación, instancia única (PID)
    grabador.py           # captura de audio con pw-record
    hotkey.py             # captura de Ctrl+Escape con XGrabKey
    transcriptor.py       # interfaz + implementación faster-whisper
    salida.py             # pegado con xclip + xdotool
config/
  config.ini              # configuración centralizada (motor, tecla, audio, …)
lanzar.sh                 # iniciar motor configurado
parar.sh                  # detener motor en marcha
model/                    # modelo VOSK (ignorado en git)
tests/                    # tests unitarios (unittest)
```
