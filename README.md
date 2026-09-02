# Estrategia de transmisión de pantalla

Este proyecto captura la pantalla del escritorio (o una ventana), la codifica en H.264 y la publica por HTTP para que otros dispositivos de la LAN la vean.

## Componentes

```
Pantalla (PipeWire / xdg-desktop-portal)
    │
    ▼
stream/screencast_stream.py  →  GStreamer  →  RTMP  →  MediaMTX
                                              (1935/tcp)
                                                    │
                                                    ▼
                                   ┌────────────────┴────────────────┐
                                   │                                 │
                              HLS :8888                       WebRTC :80
                    http://ip:8888/screen/index.m3u8      http://ip/screen
```

| Archivo / directorio | Función |
|---|---|
| `stream/screencast_stream.py` | Negocia con el portal de captura y lanza el pipeline de GStreamer. |
| `mediamtx/mediamtx` | Servidor que recibe RTMP y sirve HLS / WebRTC. |
| `mediamtx/mediamtx.yml` | Configuración de MediaMTX: puertos, protocolos, paths. |

## 1. Captura: `xdg-desktop-portal` + PipeWire

El script hace 4 pasos D-Bus con `org.freedesktop.portal.ScreenCast`:

1. **`CreateSession`** — crea una sesión de captura.
2. **`SelectSources`** — muestra el diálogo de COSMIC para elegir monitor o ventana.
3. **`Start`** — inicia el stream y devuelve un `node_id` y los metadatos (por ejemplo, `size`).
4. **`OpenPipeWireRemote`** — devuelve un file descriptor (`fd`) de PipeWire.

Parámetros importantes:

- `types: 1 | 2` — permite elegir **monitor** (`1`) o **ventana** (`2`).
- `cursor_mode: 2` — incluye el cursor embebido en el frame.
- `multiple: False` — solo un stream a la vez.

## 2. Pipeline de GStreamer

El pipeline usado en `stream/screencast_stream.py` es:

```
pipewiresrc fd=<fd> path=<node_id> do-timestamp=true
    ! video/x-raw, format=<fmt>, interlace-mode=progressive, pixel-aspect-ratio=1/1
    ! videorate
    ! video/x-raw, framerate=15/1
    ! videoconvert
    ! videoscale
    ! video/x-raw, format=I420, width=<par>, height=<impar_redondeado>
    ! queue max-size-buffers=2 leaky=downstream
    ! x264enc tune=zerolatency speed-preset=veryfast bitrate=8000
    ! h264parse config-interval=1
    ! flvmux streamable=true
    ! rtmpsink location=rtmp://127.0.0.1:1935/screen
```

### Qué hace cada paso

| Elemento / parámetro | Para qué sirve |
|---|---|
| `pipewiresrc` | Recibe los frames crudos desde PipeWire (portal). |
| `fd=<fd>` | File descriptor que devuelve el portal para la conexión PipeWire. |
| `path=<node_id>` | Identificador del nodo PipeWire. Es el formato esperado por `pipewiresrc`. |
| `do-timestamp=true` | Marca cada frame con el tiempo de reproducción. |
| `video/x-raw, format=<fmt>` | Fija el formato de color (se prueba `BGRx`, `BGRA`, `RGBx`, `RGBA` hasta que conecta). |
| `interlace-mode=progressive` | Indica que el video es progresivo, no entrelazado. |
| `pixel-aspect-ratio=1/1` | Píxeles cuadrados. |
| `videorate` | Adapta la cantidad de frames por segundo. |
| `video/x-raw, framerate=15/1` | Fija la salida a 15 fps para ahorrar CPU sin perder fluidez. |
| `videoconvert` | Cambia el formato de color si hace falta. |
| `videoscale` | Escala / redimensiona el frame al tamaño de salida. |
| `video/x-raw, format=I420, width=<par>, height=<impar_redondeado>` | Fija I420 con dimensiones pares, que es lo que `x264enc` acepta. |
| `queue max-size-buffers=2 leaky=downstream` | Buffer corto y descarte si el resto del pipeline va lento. |
| `x264enc` | Codificador H.264 por software. |
| `tune=zerolatency` | Optimiza para baja latencia. |
| `speed-preset=veryfast` | Compromiso entre calidad y CPU. Más lento = mejor calidad, más CPU. |
| `bitrate=8000` | Bitrate en kbps. Subirlo mejora la calidad. |
| `h264parse config-interval=1` | Reenvía los headers SPS/PPS en cada keyframe. |
| `flvmux streamable=true` | Muxa en formato FLV para RTMP. |
| `rtmpsink location=...` | Envía el stream a MediaMTX por RTMP. |

### Variables clave en el script

```python
TARGET_FPS = 15        # fps de salida
VIDEO_BITRATE = 8000   # kbps
X264_PRESET = "veryfast"
enc_width = (width + 1) // 2 * 2   # fuerza ancho par
enc_height = (height + 1) // 2 * 2 # fuerza alto par
```

La resolución de salida se fija al **tamaño inicial** de la fuente redondeado a pares. Si redimensionás la ventana después, `videoscale` la adapta sin cortar el stream.

## 3. MediaMTX

MediaMTX recibe el flujo RTMP en `rtmp://127.0.0.1:1935/screen` y lo reemite por HTTP.

### Configuración de ejemplo

```yaml
rtmp: true
rtmpAddress: :1935

hls: true
hlsAddress: :8888

webrtc: true
webrtcAddress: :80
webrtcLocalUDPAddress: :8189
```

| Servicio | Puerto | URL de consumo |
|---|---|---|
| RTMP (publicación) | `1935/tcp` | `rtmp://127.0.0.1:1935/screen` |
| HLS | `8888/tcp` | `http://<ip>:8888/screen/index.m3u8` |
| WebRTC | `80/tcp` + `8189/udp` | `http://<ip>/screen` |

### Si usás el puerto 80

El puerto 80 es privilegido. Para que MediaMTX lo use sin correrlo como root:

```bash
sudo setcap cap_net_bind_service=+ep /home/willy/classroom-stream/mediamtx/mediamtx
```

Y luego ejecutarlo normalmente:

```bash
cd /home/willy/classroom-stream/mediamtx
./mediamtx
```

### Firewall (`ufw`)

```bash
sudo ufw allow 1935/tcp    # RTMP
sudo ufw allow 8888/tcp    # HLS
sudo ufw allow 80/tcp      # WebRTC HTTP
sudo ufw allow 8189/udp    # WebRTC ICE
```

## 4. Uso

1. Levantar MediaMTX:

```bash
cd /home/willy/classroom-stream/mediamtx
./mediamtx
```

2. Iniciar la captura:

```bash
python3 /home/willy/classroom-stream/stream/screencast_stream.py
```

Elegir la pantalla completa o la ventana en el diálogo de COSMIC.

3. Abrir en el navegador de otro dispositivo:

```text
http://192.168.7.8/screen          # WebRTC
http://192.168.7.8:8888/screen/index.m3u8  # HLS
```

## 5. Ajustes comunes

### Mejorar calidad

- Elegir **pantalla completa** en vez de una ventana pequeña.
- Subir `VIDEO_BITRATE` (por ejemplo, `12000`).
- Bajar `TARGET_FPS` (por ejemplo, `10`) para liberar CPU y destinar más bits por frame.
- Usar un `X264_PRESET` más lento (`faster`, `fast`, `medium`) si la CPU da abasto.

### Redimensionar la fuente

El stream sigue andando si se redimensiona la ventana, pero la resolución de salida queda fija en la del inicio. Para cambiar la resolución, cortar con `Ctrl+C` y volver a iniciar el script con la ventana en el tamaño deseado.

## 6. Apagar todo

### Cortar la transmisión

En la terminal donde corre `screencast_stream.py`:

```text
Ctrl+C
```

Eso envía la señal `KeyboardInterrupt`, el script baja el pipeline y cierra el fd de PipeWire.

### Parar MediaMTX

Si lo corriste a mano en primer plano:

```text
Ctrl+C
```

Si lo corriste en segundo plano, buscá el proceso y matalo:

```bash
pgrep -a mediamtx
pkill mediamtx
```

O, si lo corriste con `sudo`:

```bash
sudo pkill mediamtx
```

### Cerrar el puerto del firewall

Si más tarde no vas a usar un puerto, podés cerrarlo. Por ejemplo, para WebRTC:

```bash
sudo ufw delete allow 80/tcp
sudo ufw delete allow 8189/udp
```

### Limpieza rápida (todo junto)

```bash
pkill -f screencast_stream.py
pkill mediamtx
```

Después de esto no queda nada corriendo ni escuchando en los puertos.

## 7. Instalación y uso en red

Este modo permite tener un **servidor central** que recibe la pantalla de uno o dos **docentes** y la sirve por HLS a más de 10 **alumnos**.

### En el servidor

1. Clonar o copiar el proyecto en la máquina servidor.
2. Verificar que los puertos estén abiertos en el firewall:

```bash
sudo ufw allow 1935/tcp    # RTMP (docentes publican)
sudo ufw allow 8888/tcp    # HLS (alumnos reproducen)
sudo ufw allow 8090/tcp    # Página web del aula
```

3. Iniciar el servidor:

```bash
./classroom-server start
```

Eso levanta MediaMTX y un servidor HTTP en `http://<ip-del-servidor>:8090`.

### En la máquina del docente

1. Clonar o copiar el proyecto.
2. Iniciar la transmisión indicando la IP del servidor y el identificador del docente:

```bash
STREAM_SERVER=192.168.1.10:1935 TEACHER_ID=teacher1 ./stream-screen start
STREAM_SERVER=192.168.42.105:1935 TEACHER_ID=teacher1 VIDEO_BITRATE=16000 X264_PRESET=faster TARGET_FPS=15 ./stream-screen start
```

Para el segundo docente, usar otro `TEACHER_ID`:

```bash
STREAM_SERVER=192.168.1.10:1935 TEACHER_ID=teacher2 ./stream-screen start
```

3. Para cortar:

```bash
./stream-screen stop
```

### En la máquina del alumno

1. Abrir el navegador en `http://<ip-del-servidor>:8090`.
2. Elegir el docente y copiar el enlace M3U8, o abrirlo directamente si el navegador soporta HLS (Safari).
3. También se puede reproducir con VLC, IINA o cualquier reproductor HLS:

```text
http://<ip-del-servidor>:8888/teacher1/index.m3u8
http://<ip-del-servidor>:8888/teacher2/index.m3u8
```

### Notas de escala

- HLS (puerto `8888`) es el recomendado para más de 10 alumnos porque se sirve como HTTP puro.
- WebRTC (puerto `80`) consume más recursos del servidor; evitarlo para muchos espectadores.
