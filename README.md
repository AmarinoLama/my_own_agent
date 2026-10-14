# Agente: memoria + voz + orbe

Un único proyecto con un agente que **recuerda entre sesiones**, entiende el
español y responde siempre en español. También puedes hablarle por voz mediante
una mini-barra negra fija en el borde superior de la pantalla con **5 puntos de
frecuencia** (esquinas inferiores redondeadas y codos invertidos que la sueldan
al borde, sin texto): azules y creciendo con tu voz al escuchar, girando en
círculo al pensar, y de nuevo en línea pero naranjas y moviéndose como si
hablara cuando te contesta. En pausa se quedan grises con el símbolo ⏸ debajo;
un clic en la barra pausa y reanuda.

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
| Calendario | `calendario.py` | Eventos locales y widget mensual |
| Recursos | `assets/` | Iconos y mascota del thinking orb |

## Uso

```bash
python main.py                         # chat por consola
python main.py --voz                   # modo voz con orbe
python chat.py                         # chat del agente con memoria
python chat.py -o "¿qué sabes de mí?"
python orbe.py                         # orbe del agente
python orbe.py --texto                 # orbe escribiendo por consola
python orbe.py --acceso-directo        # crea el icono Agente y Ctrl+Alt+A
python orbe.py --instalar-palmadas     # doble palmada: abre Jarvis, también tras reiniciar
python orbe.py --detener-palmadas      # detiene el vigilante activo
python voz.py --nivel                  # comprueba el nivel del micrófono
python voz.py --aprender-voz            # crea tu perfil local de hablante (12 s)
python chat_cli.py --models            # lista modelos disponibles
python orbe_art.py                     # regenera el arte: iconos .ico y mascota
```

El acceso directo **Agente** se crea con `Ctrl+Alt+A` y funciona desde cualquier
ventana de Windows. Si quieres usar otra combinación:

```bash
python orbe.py --acceso-directo --tecla CTRL+ALT+Q
```

Si ya tenías un acceso directo creado antes de reorganizar el proyecto, vuelve a
ejecutar `python orbe.py --acceso-directo`: actualizará la ruta y la tecla rápida.

El icono se dibuja al crear el acceso directo: un thinking orb de cristal en
varios tamaños (de 16 a 256 px), verde para **Agente** y azul para **Voz**.
Windows guarda los iconos en caché; si no cambia a la primera, cierra sesión y
vuelve a entrar o ejecuta `ie4uinit.exe -show`. El mismo orbe es la mascota de
`assets/mascota/`, con una animación por estado; ambos se regeneran con
`python orbe_art.py`.


## Calendario por voz

En el modo voz puedes pedir «muéstrame el calendario»: se abre una ventana
mensual flotante que puedes explorar con el ratón. Añade o consulta citas
hablándole al agente; no hace falta teclear ni editar la vista. Ejemplos:

```text
Apunta dentista el 14 de mayo a las 10:30
¿Qué tengo esta semana?
¿Qué eventos tengo el 14 de mayo?
Muéstrame el calendario
```

Los eventos se guardan localmente en `calendario.json` y el widget los refleja
automáticamente. Si dices una fecha sin año, el agente confirma la fecha completa
que va a guardar. La hora es opcional. No hay sincronización externa ni alarmas
en esta primera versión; para borrar un evento, pide que lo elimine indicando la
cita concreta.

El modo voz reconoce español de España (`es-ES`) y usa **Edge TTS** con la voz
neuronal española `es-ES-AlvaroNeural`. La interfaz del agente son **5 puntos de
frecuencia**, no una cara: crecen y encogen con el sonido, se alinean en azul
cuando hablas tú, dan vueltas en círculo cuando piensa y se vuelven a alinear en
naranja moviéndose al ritmo de la voz cuando te contesta (rojos si hay un error).
Se pintan a cuatro veces el tamaño con Pillow y se reducen para que los halos
queden suaves; si falta Pillow, se dibujan directamente en el lienzo de Tk.

La síntesis de Edge TTS es gratuita, no necesita API key y requiere conexión a
Internet. Puedes cambiar el idioma del reconocimiento con `--idioma`, por ejemplo:

```bash
python orbe.py --idioma es-ES --micro 2
```

### Hacer más fiable la escucha

La captura se segmenta en marcos de 20 ms y decide si hay voz con Silero VAD
(red ligera sobre torch, `pip install silero-vad`, ya instalada); si falta, usa
WebRTC VAD (`webrtcvad`) y, en último caso, un umbral de energía que se adapta
al ruido de la habitación. Para que un ventilador no corte ni abra frases hace
falta voz sostenida (3 de los 6 últimos marcos), la frase arranca con 0,3 s de
preroll para no perder la primera sílaba y se cierra a los `--silencio` segundos
de silencio (0,7 por defecto), con un tope de 20 s por si el ruido no para.

En Windows, Edge TTS
reproduce los MP3 mediante WinMM/MCI en lugar de inicializar pygame con una
frecuencia fija. Para transcripción local en español puedes instalar
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (la descarga del
modelo ocurre al primer uso):

```bash
pip install faster-whisper
python orbe.py --transcripcion whisper --modelo-whisper small
```

`base` usa menos recursos; `small` suele reconocer mejor a costa de más RAM y
CPU. Si dejas `--transcripcion auto` (por defecto), prueba Whisper local si está
instalado y vuelve a Google en español si falta o no carga. Para forzar Google:

```bash
python orbe.py --transcripcion google
```

La opción `--silencio 0.65` determina cuánto silencio espera al final de cada
frase. `--micro` permite elegir el dispositivo de entrada. Para activar Jarvis
con dos palmadas aunque la ventana esté cerrada, ejecuta una vez
`python orbe.py --instalar-palmadas`. Se crea un acceso directo en el inicio de
sesión de Windows y el detector queda en segundo plano; usa
`python orbe.py --desinstalar-palmadas` para quitarlo. También puedes arrancar el
vigilante solo para la sesión con `python orbe.py --vigilar-palmadas`.

El detector mide localmente picos cortos de PCM y busca dos separados entre
0.22 y 1.25 segundos: no usa IA, no transcribe, no guarda ni envía audio. El
micrófono solo lo tiene abierto mientras Jarvis está cerrado: en cuanto arranca
lo suelta, y si la ventana ya está abierta el detector no se mete con el micro.
El umbral se calibra al arrancar y se puede fijar con
`AGENTE_PALMADA_UMBRAL` en `.env` si detecta ruido ambiental como palmadas o no
llega a reconocerlas; sin eso, el mínimo por defecto es 8000 y en una habitación
ruidosa sube a cinco veces el pico de fondo, para que los clics del ratón o el
teclado no cuenten como palmadas. Además, durante los 8 segundos siguientes a
una apertura o a un cierre el vigilante ignora el micro: si cierras con el
ratón, sus propios clics no lo reabren. Si abres el orbe mientras ya hay otro
en marcha no se hace una copia: se trae la ventana existente al frente.
Si el micro que tengas elegido está mudo (cascos
desconectados o apagados) el vigilante lo nota y se pasa al siguiente que capte
algo, sin tocar tu elección para cuando vuelvas a conectarlos.
`python orbe.py --detener-palmadas` apaga el vigilante de
esta sesión sin desinstalarlo.

Para apagar Jarvis con la voz, di «ciérrate, Jarvis» (o «hasta luego,
Jarvis»): se despide, cierra la ventana y deja el micro libre. No vuelve a
abrirse solo: hasta que des dos palmadas. Lo mismo hace el menú del clic
derecho > Salir.

Si al conectar unos
cascos cambia el micro predeterminado, prueba `python orbe.py --listar-micros`
y elige una entrada diferente: el micrófono Bluetooth manos libres puede hacer
que Windows cambie también el perfil de salida del casco.

### Cambiar la voz española gratuita

Puedes probar otras voces disponibles de Edge TTS desde la consola:

```bash
edge-tts --list-voices
```

Por ejemplo, para usar una voz femenina española:

```env
VOZ_MOTOR=edge
EDGE_TTS_VOICE=es-ES-ElviraNeural
```

También puedes elegir el motor directamente:

```bash
python orbe.py --voz-motor edge
```

Si Edge TTS no tiene conexión, el programa vuelve automáticamente a SAPI.

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
error, se registra en la consola y en `voz.log` (el orbe solo cambia a estado de error); para volver a la voz local de Windows usa `VOZ_MOTOR=sapi` o `python orbe.py --voz-motor sapi`.

La integración solicita audio WAV para reproducirlo directamente en Windows y
no guarda el audio ni la clave en archivos adicionales.

## Control seguro del PC

El agente puede ejecutar algunas acciones concretas cuando se lo pides, tanto en
`chat.py` como en el orbe:

- abrir Spotify de escritorio, WhatsApp, el navegador, la calculadora o el Bloc de notas;
- buscar una canción, artista o playlist en Spotify;
- abrir Spotify de escritorio y, cuando se pide reproducir, intentar seleccionar la primera coincidencia con teclado/ratón;
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

La búsqueda de Spotify prioriza la aplicación de escritorio mediante
`spotify:search:`; al pedir reproducir, el agente puede intentar enfocar Spotify,
escribir la búsqueda y seleccionar la primera coincidencia con teclado/ratón.
Solo actúa sobre una ventana cuyo proceso es `spotify.exe`; no controla el ratón
de forma general. Como la interfaz puede cambiar, siempre informa de que lo ha
intentado y no afirma que la canción esté sonando sin confirmación.

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

### Reconocer tu voz e ignorar música

Para que el agente distinga tu voz de música, vídeos u otras personas, crea una
huella local una sola vez:

```bash
python voz.py --aprender-voz --micro 2
```

Habla de forma natural durante unos 12 segundos y no pongas música durante la
grabación. Se guardará `voz_perfil.npy`, que está excluido de Git. Al iniciar el
orbe, cada frase se comparará con esa huella antes de enviarse a Google o al
modelo; si no coincide, se ignora. El modelo local se descarga la primera vez y
la comparación se hace en tu PC. Para desactivarlo temporalmente:

```bash
python orbe.py --sin-verificacion
```

No es una garantía biométrica perfecta: ruido muy fuerte, una grabación de tu
voz o hablar muy lejos del micrófono pueden producir falsos positivos o
negativos. Para mejores resultados, vuelve a crear el perfil con `--aprender-voz`
en el mismo lugar y usando el mismo MHW-100.

### Interrumpir al agente

Mientras el agente está hablando, puedes empezar a hablarle. Se acumulan marcos
de audio de 20 ms, se exige voz sostenida y se espera el silencio de fin de frase
antes de interrumpir; golpes sueltos no deberían cortarlo. Puedes escoger
`--transcripcion whisper` para hacerlo localmente o usar el modo `auto` que
tiene Google como respaldo. Si quieres que además ignore otras voces, crea un
perfil local con `python voz.py --aprender-voz --micro 2`.

En Windows, Edge TTS usa el reproductor MP3 nativo MCI para evitar fijar a la
fuerza 48 kHz/estéreo en pygame. También se puede seleccionar el backend:
`VOZ_EDGE_BACKEND=auto` (predeterminado), `mci` (forzar WinMM) o `pygame`.
El cambio intenta eliminar una causa de zumbido de software, pero no confirma
que el driver o el perfil Bluetooth del casco estén bien; si persiste, indica el
modelo/conexión del casco y qué entrada/salida selecciona Windows.

## Memoria

La memoria son ficheros Markdown dentro de `memoria/`, sin base de datos. El
agente los lee en cada turno y puede guardarlos mediante herramientas. Además,
al cerrar la sesión revisa la conversación y conserva solo información duradera
que hayas dicho explícitamente: preferencias, proyectos, decisiones, objetivos
y lecciones. No guarda claves API ni conversaciones completas.

Si dices “recuerda”, “apunta” o “anota”, el agente intenta guardarlo en el mismo
turno; si no lo hace, se conserva literalmente en `memoria/recuerdos-sueltos.md`.
`/reset` guarda lo aprendido antes de limpiar el historial de la conversación.

## Cardpilot: optimizador de compras Cardmarket

Cardpilot es una aplicación **Angular** con un pequeño servidor local Python.
Agrupa wants del grupo y combina vendedores por precio, stock, trend y portes;
maximiza las copias cubiertas y, a igual cobertura, la diferencia frente al
trend. Reparte el envío entre las personas que efectivamente compran a cada
tienda y muestra el pedido por vendedor/persona. Exporta un Excel con resumen,
órdenes, reparto individual y cartas pendientes.

Instala dependencias/build una vez y arranca:

En Windows, basta con ejecutar **`cardmarket.bat`**. Instala las dependencias locales y compila Angular en el primer arranque; después abre `http://127.0.0.1:8765`. Deja la ventana abierta mientras uses Cardpilot. En otros sistemas, o para preparar el build manualmente:

```bash
npm install --prefix cardmarket-angular
npm run build --prefix cardmarket-angular
python cardmarket.py
```

Para otro puerto: `python cardmarket.py --port 8766` (la extensión incluida está configurada para 8765). Los datos manuales se conservan en localStorage de ese navegador. También se pueden guardar/importar JSON.

### Importar una página abierta en Firefox

1. En Cardpilot, abre **Ver opciones → Descargar extensión** o descomprime el ZIP incluido desde el botón.
2. En Firefox entra en `about:debugging#/runtime/this-firefox` → **Cargar complemento temporal** y selecciona `manifest.json` de la carpeta descomprimida `cardmarket-extension/`.
3. Inicia sesión en Cardmarket en la pestaña normal de Firefox y abre una lista de wants o una página con ofertas visibles.
4. Pulsa el icono Cardpilot, identifica la persona de la lista, revisa la previsualización y pulsa **Importar**. La app local recibe solo las filas que se vieron en la vista.
5. Revisa edición/idioma/condición, trend y envío antes de optimizar. Cardmarket calcula el porte final según destino y contenido del carrito; la extensión no lo inventa. Cuando varias personas quieran importar listas, repite el paso por persona.

La extensión requiere un clic por importación: no vigila la cuenta ni rastrea navegación en segundo plano. El DOM de Cardmarket puede cambiar; si no se detectan filas, copia/importa datos manualmente. Importar una página con ofertas visibles es experimental: vendedor/precio pueden requerir revisión y no equivale a consultar toda la oferta del marketplace.

**Límite real de integración:** Cardmarket indica que no está aceptando aplicaciones nuevas para su API. El aviso oficial dejó la API y el Shopping Wizard desactivados «hasta nuevo aviso». Por tanto no hay una vía autorizada para una sincronización server-to-server ni para obtener en tiempo real todas las ofertas de tus amigos; tampoco es apropiado sortear el login o raspar endpoints privados. Esta extensión es una importación visible, explícita y bajo tu control, no una garantía de acceso a toda la cuenta. No solicita ni guarda credenciales, cookies, mensajes o datos bancarios. No hace pedidos; siempre los confirmas tú.

## Configuración

Copia `.env.example` a `.env` y añade tu clave. El `.env` real está ignorado por
Git y no debe publicarse:

```env
API_CHATGPT=thk_live_...
API_CHATGPT_BASE_URL=https://tokenharbor.ai/v1
API_CHATGPT_MODEL=deepseek-v4.1-flash:free
API_CHATGPT_FALLBACK=deepseek-v4.1-flash:free
AGENTE_NOMBRE=Agente
AGENTE_IDIOMA=es-ES
# AGENTE_MEMORIA=./memoria
```

La clave va en la cabecera `Authorization: Bearer` contra
`https://tokenharbor.ai/v1/chat/completions`. Si la API responde
`email_verification_required`, confirma tu correo en
<https://tokenharbor.ai/dashboard> antes de usarla.

Instala las dependencias en tu entorno virtual:

```bash
pip install openai python-dotenv pywin32 SpeechRecognition pyaudio edge-tts pygame pyautogui resemblyzer webrtcvad-wheels silero-vad
```

`silero-vad` instala torch por su cuenta: es el VAD principal de la escucha
(si no está, manda WebRTC o la energía adaptativa).

`faster-whisper` es opcional; instálalo solo si quieres reconocimiento local:

```bash
pip install faster-whisper
```

Si no necesitas voz, el chat por consola solo requiere `openai` y
`python-dotenv`. No compartas nunca el contenido de `.env`.
