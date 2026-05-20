# Escritura por voz

Dictado de voz offline para Linux en español. Reconoce lo que dices y lo escribe directamente donde tengas el foco, sin servicios en la nube.

## Tecnologías

| Tecnología                                         | Rol                                                                          |
| -------------------------------------------------- | ---------------------------------------------------------------------------- |
| [VOSK](https://alphacephei.com/vosk/)              | Motor de reconocimiento de voz offline                                       |
| Modelo `vosk-model-es-0.42`                        | Modelo grande de español, entrenado a 16 kHz                                 |
| [PipeWire](https://pipewire.org/)                  | Captura de audio del micrófono (`pw-cat`)                                    |
| [xdotool](https://github.com/jordansissel/xdotool) | Simulación de teclado y pegado en X11                                        |
| [xclip](https://github.com/astrand/xclip)          | Portapapeles — permite escribir tildes y caracteres especiales correctamente |
| Python 3                                           | Lenguaje del script principal                                                |

## Cómo funciona

1. `lanzar.sh` arranca el proceso de reconocimiento en segundo plano
2. El micrófono captura audio vía PipeWire a 16 kHz
3. VOSK transcribe el audio localmente usando el modelo español
4. El texto reconocido se copia al portapapeles con `xclip`
5. `xdotool` simula `Ctrl+V` para pegarlo donde esté el foco activo
6. `parar.sh` detiene el proceso

El uso del portapapeles para escribir (en lugar de simular tecla a tecla) resuelve el problema de las tildes y caracteres especiales en teclados con dead keys.

## Uso

```sh
./lanzar.sh   # iniciar dictado
./parar.sh    # detener dictado
```

## Requisitos

```sh
# Sistema
sudo apt install xdotool xclip python3 python3-venv

# PipeWire (normalmente ya instalado en distros modernas)
sudo apt install pipewire
```

El primer arranque instala automáticamente `vosk` en un entorno virtual local (`.venv/`). Los siguientes arranques son inmediatos.

## Estructura

```
app/
  nerd-dictation        # script principal (Python)
  config/
    nerd-dictation.py   # configuración: espaciado entre segmentos
  examples/
    default/            # ejemplo de reemplazos de palabras
    vosk_grammar/       # ejemplo de gramática restringida
lanzar.sh               # iniciar reconocimiento
parar.sh                # detener reconocimiento
model/                  # modelo de lenguaje español (ignorado en git)
```
