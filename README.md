# Agente: memoria + voz + orbe

Un único proyecto con un agente que **recuerda entre sesiones**, entiende el
español y responde siempre en español. También puedes hablarle por voz mediante
un orbe flotante que permanece encima de las demás ventanas.

## Estructura

| Pieza | Archivo o carpeta | Función |
| --- | --- | --- |
| Entrada principal | `main.py` | Abre el chat o el modo voz con `--voz` |
| Cerebro | `cerebro.py` | Modelo, herramientas y memoria persistente |
| Chat | `chat.py` | Conversación por consola con memoria |
| Orbe | `orbe.py` | Entrada del agente con orbe y voz |
| Voz | `voz.py` | Micrófono, selector, orbe y voz de Windows |
| Cliente API | `chat_cli.py` | Cliente OpenAI-compatible y métricas |
| Memoria | `memoria/*.md` | Notas editables que sobreviven entre sesiones |
| Recursos | `assets/` | Iconos del orbe |

## Uso

```bash
python main.py                         # chat por consola
python main.py --voz                   # modo voz con orbe
python chat.py                         # chat del agente con memoria
python chat.py -o "¿qué sabes de mí?"
python orbe.py                         # orbe del agente
python orbe.py --texto                 # orbe escribiendo por consola
python orbe.py --acceso-directo        # crea el icono Agente y Ctrl+Alt+A
python voz.py --nivel                  # comprueba el nivel del micrófono
python chat_cli.py --models            # lista modelos disponibles
```

El acceso directo **Agente** se crea con `Ctrl+Alt+A` y funciona desde cualquier
ventana de Windows. Si quieres usar otra combinación:

```bash
python orbe.py --acceso-directo --tecla CTRL+ALT+Q
```

Si ya tenías un acceso directo creado antes de reorganizar el proyecto, vuelve a
ejecutar `python orbe.py --acceso-directo`: actualizará la ruta y la tecla rápida.


El modo voz reconoce español de España (`es-ES`) por defecto y usa la voz
de Windows que coincida con `--voz-salida` (por defecto, `David`, voz masculina).
Puedes cambiar el idioma del reconocimiento con `--idioma`, por ejemplo:

```bash
python orbe.py --idioma es-ES --micro 2
```

### Usar tu voz de Fish Audio

La IA conversacional sigue siendo la de `API_CHATGPT`; Fish Audio solo se usa
para convertir sus respuestas en voz con tu voz de Rick Sánchez. Necesitas dos
datos de Fish Audio:

1. Crea o copia una API key desde tu cuenta de Fish Audio.
2. Copia el **Voice ID/Model ID** de la voz que quieres utilizar (normalmente
   aparece en la página de esa voz).

Ponlos en el `.env` de la raíz, que nunca debes publicar:

```env
VOZ_MOTOR=fish
FISH_AUDIO_API_KEY=tu_clave_de_fish_audio
FISH_AUDIO_VOICE_ID=el_id_de_tu_voz
```

Después inicia el orbe normalmente:

```bash
python orbe.py
# o, para probarlo sin cambiar el .env:
python orbe.py --voz-motor fish
```

También puedes pasar el identificador directamente:

```bash
python orbe.py --voz-motor fish --voz-salida=EL_ID_DE_TU_VOZ
```

El reconocimiento de tu micrófono sigue usando Google en español (`es-ES`),
y solo las respuestas del agente se generan en Fish Audio. Si Fish devuelve un
error, el orbe lo muestra en pantalla y lo registra en `voz.log`; para volver a
Helena usa `VOZ_MOTOR=sapi` o `python orbe.py --voz-motor sapi`.

La integración solicita audio WAV para reproducirlo directamente en Windows y
no guarda el audio ni la clave en archivos adicionales.

## Control seguro del PC

El agente puede ejecutar algunas acciones concretas cuando se lo pides, tanto en
`chat.py` como en el orbe:

- abrir Spotify, WhatsApp, el navegador, la calculadora o el Bloc de notas;
- buscar una canción, artista o playlist en Spotify;
- preparar un mensaje de WhatsApp con el número y el texto indicados.

WhatsApp tiene una confirmación obligatoria: el agente prepara el mensaje, pero
no abre el enlace hasta que confirmes claramente. Después se abre WhatsApp con el
texto escrito y **tú tienes que revisar y pulsar Enviar**. No se envían mensajes
automáticamente. Tampoco se ejecutan comandos de terminal ni se abren rutas
arbitrarias dadas por el modelo.

Ejemplos:

```text
Abre Spotify y busca Daft Punk
Abre la calculadora
Prepara un WhatsApp para +34600000000 diciendo: llego en diez minutos
Sí, abre el WhatsApp preparado
```

La búsqueda de Spotify abre la página o aplicación de Spotify con los resultados;
la reproducción queda a un clic para evitar que la IA reproduzca contenido por
sorpresa.

### Escritorios guardados

Puedes registrar la disposición actual de tus ventanas con una orden como:

```text
Oye Rick, registra el escritorio trabajo
```

y recuperarla después con:

```text
Oye Rick, abre el escritorio trabajo
```

Los registros se guardan como JSON en `escritorios/` e incluyen aplicaciones,
títulos visibles, posiciones, tamaños y si estaban minimizadas o maximizadas. Al
restaurar, el agente reabre los ejecutables que Windows permita y recoloca las
ventanas; no cierra ventanas que ya estuvieran abiertas.

Windows no permite leer normalmente las pestañas internas ni las URL de Firefox
mediante la API básica de ventanas. Por eso se recupera la ventana de Firefox y
su tamaño, pero no se garantiza la reconstrucción exacta de cada pestaña.

### Interrumpir al agente

Mientras el agente está hablando, puedes empezar a hablarle. El orbe mantiene una
escucha breve del micrófono seleccionado; cuando detecta tu frase y te callas,
corta la respuesta actual, transcribe lo nuevo en español y lo procesa como el
siguiente turno. Con el oído de Google puede tardar unas décimas porque necesita
esperar al final de tu frase; con SAPI el reconocimiento puede ser más inmediato.

## Memoria

La memoria son ficheros Markdown dentro de `memoria/`, sin base de datos. El
agente los lee en cada turno y puede guardarlos mediante herramientas. Además,
al cerrar la sesión revisa la conversación y conserva solo información duradera
que hayas dicho explícitamente: preferencias, proyectos, decisiones, objetivos
y lecciones. No guarda claves API ni conversaciones completas.

Si dices “recuerda”, “apunta” o “anota”, el agente intenta guardarlo en el mismo
turno; si no lo hace, se conserva literalmente en `memoria/recuerdos-sueltos.md`.
`/reset` guarda lo aprendido antes de limpiar el historial de la conversación.

## Configuración

Copia `.env.example` a `.env` y añade tu clave. El `.env` real está ignorado por
Git y no debe publicarse:

```env
API_CHATGPT=sk-...
API_CHATGPT_BASE_URL=https://api.xkiro.com/v1
API_CHATGPT_MODEL=mistralai/mistral-large-2512
API_CHATGPT_FALLBACK=qwen/qwen3.7-plus:free
AGENTE_NOMBRE=Agente
AGENTE_IDIOMA=es-ES
# AGENTE_MEMORIA=./memoria
```

Instala las dependencias en tu entorno virtual:

```bash
pip install openai python-dotenv pywin32 SpeechRecognition pyaudio
```

Si no necesitas voz, el chat por consola solo requiere `openai` y
`python-dotenv`. No compartas nunca el contenido de `.env`.
