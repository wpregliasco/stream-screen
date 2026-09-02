#!/usr/bin/env python3
"""
Captura de pantalla vía xdg-desktop-portal ScreenCast (COSMIC) -> GStreamer -> RTMP -> MediaMTX

Requiere:
    sudo apt install python3-dbus python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-vaapi

Uso:
    python3 screencast_stream.py
    (COSMIC va a mostrar el selector de pantalla/ventana la primera vez que corras esto)
"""

import os
import sys
import random
import socket
import dbus
import dbus.mainloop.glib
from gi.repository import GLib


def _get_lan_ip():
    """Devuelve la IP de la interfaz usada por la ruta por defecto."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"

bus = dbus.SessionBus()
portal = bus.get_object(PORTAL_BUS_NAME, PORTAL_OBJECT_PATH)
screencast = dbus.Interface(portal, SCREENCAST_IFACE)

# nombre único del bus, escapado como pide la spec del portal
sender_escaped = bus.get_unique_name()[1:].replace(".", "_")

loop = GLib.MainLoop()
state = {}


def new_token():
    return f"gst_{random.randint(100000, 999999)}"


def request_path(token):
    return f"/org/freedesktop/portal/desktop/request/{sender_escaped}/{token}"


def wait_response(token, callback):
    path = request_path(token)

    def on_response(response, results):
        bus.remove_signal_receiver(on_response, "Response", REQUEST_IFACE,
                                    PORTAL_BUS_NAME, path)
        if response != 0:
            print(f"ERROR: el portal devolvió response={response} (cancelado o falló)")
            loop.quit()
            sys.exit(1)
        callback(results)

    bus.add_signal_receiver(on_response, "Response", REQUEST_IFACE,
                             PORTAL_BUS_NAME, path)


def step1_create_session():
    print("1/4 - CreateSession...")
    token = new_token()
    session_token = new_token()
    state["session_token"] = session_token

    def on_created(results):
        state["session_handle"] = results["session_handle"]
        step2_select_sources()

    wait_response(token, on_created)
    screencast.CreateSession({
        "handle_token": token,
        "session_handle_token": session_token,
    })


def step2_select_sources():
    print("2/4 - SelectSources (elegí pantalla en el diálogo que aparezca)...")
    token = new_token()

    def on_selected(results):
        step3_start()

    wait_response(token, on_selected)
    screencast.SelectSources(state["session_handle"], {
        "handle_token": token,
        "types": dbus.UInt32(1 | 2),   # 1=MONITOR, 2=WINDOW
        "cursor_mode": dbus.UInt32(2), # 2=EMBEDDED (cursor incluido en el frame)
        "multiple": False,
    })


def step3_start():
    print("3/4 - Start...")
    token = new_token()

    def on_started(results):
        streams = results["streams"]
        node_id = int(streams[0][0])
        state["node_id"] = node_id
        state["stream_meta"] = streams[0][1] if len(streams[0]) > 1 else {}
        print(f"    node_id asignado por el portal: {node_id}")
        print(f"    metadatos del stream: {state['stream_meta']}")
        step4_open_pipewire_remote()

    wait_response(token, on_started)
    screencast.Start(state["session_handle"], "", {"handle_token": token})


def step4_open_pipewire_remote():
    print("4/4 - OpenPipeWireRemote...")
    fd_obj = screencast.OpenPipeWireRemote(state["session_handle"], {},
                                            dbus_interface=SCREENCAST_IFACE)
    fd = fd_obj.take()  # toma ownership del fd, no lo cierra dbus-python
    state["fd"] = fd
    print(f"    fd obtenido: {fd}")
    loop.quit()


step1_create_session()
loop.run()

# --- Handshake terminado, arrancamos GStreamer en este mismo proceso ---

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)

node_id = state["node_id"]
fd = state["fd"]
meta = state.get("stream_meta", {})

# Intentamos extraer el tamaño real del metadato del portal, sino usamos 1920x1080@30
width, height = 1920, 1080
framerate = "30/1"

try:
    size = meta.get("size") or meta.get("Size")
    if size:
        width = int(size[0])
        height = int(size[1])
except Exception as e:
    print(f"    no se pudo leer 'size' del portal: {e}")

RTMP_URL = os.environ.get("RTMP_URL", "rtmp://127.0.0.1:1935/screen")
LAN_IP = _get_lan_ip()

# x264 con formato I420 requiere ancho y alto pares. Redondeamos el
# tamaño de la fuente para evitar el error "Can not initialize x264 encoder"
# cuando se elige una ventana de dimensiones impares.
enc_width = (width + 1) // 2 * 2
enc_height = (height + 1) // 2 * 2
if (width, height) != (enc_width, enc_height):
    print(f"    redondeando tamaño a pares: {enc_width}x{enc_height}")

# Menos fps para ahorrar CPU; más bitrate y mejor preset para mejor calidad.
TARGET_FPS = 15
VIDEO_BITRATE = 8000
X264_PRESET = "veryfast"  # alternativas: superfast, veryfast, faster

# Probamos varios formatos fijos hasta que pipewiresrc negocie.
# El error original era que pipewiresrc no recibía caps fijas aguas abajo
# (handle_format_change: assertion 'gst_caps_is_fixed (pwsrc->caps)' failed).
# Fijamos el formato, ancho y alto de la pantalla capturada.
formats = ["BGRx", "BGRA", "RGBx", "RGBA"]

# 'path' es el identificador del portal; 'target-object' es una alternativa
# si en alguna versión de GStreamer se elimina 'path'.
id_props = ["path", "target-object"]

candidates = []

for id_prop in id_props:
    for fmt in formats:
        candidates.append((
            f"x264 {id_prop} {fmt}",
            (
                f"pipewiresrc fd={fd} {id_prop}={node_id} do-timestamp=true ! "
                f"video/x-raw, format={fmt}, "
                f"interlace-mode=progressive, pixel-aspect-ratio=1/1 ! "
                f"videorate ! "
                f"video/x-raw, framerate={TARGET_FPS}/1 ! "
                f"videoconvert ! "
                f"videoscale ! "
                f"video/x-raw, format=I420, width={enc_width}, height={enc_height} ! "
                f"queue max-size-buffers=2 leaky=downstream ! "
                f"x264enc tune=zerolatency speed-preset={X264_PRESET} bitrate={VIDEO_BITRATE} ! "
                f"h264parse config-interval=1 ! "
                f"flvmux streamable=true ! "
                f"rtmpsink location={RTMP_URL}"
            )
        ))

# VAAPI como último recurso con el formato más común
for id_prop in id_props:
    candidates.append((
        f"vaapi {id_prop} BGRx",
        (
            f"pipewiresrc fd={fd} {id_prop}={node_id} do-timestamp=true ! "
            f"video/x-raw, format=BGRx, width={width}, height={height}, "
            f"interlace-mode=progressive, pixel-aspect-ratio=1/1 ! "
            f"videoconvert ! "
            f"vaapipostproc ! "
            f"video/x-raw(memory:VASurface), format=NV12 ! "
            f"queue max-size-buffers=2 leaky=downstream ! "
            f"vaapih264enc rate-control=cbr bitrate=4000 ! "
            f"h264parse config-interval=1 ! "
            f"flvmux streamable=true ! "
            f"rtmpsink location={RTMP_URL}"
        )
    ))

pipeline = None
active_pipeline_str = None

for name, pipeline_str in candidates:
    print(f"\nProbando pipeline: {name}")
    print(pipeline_str)
    try:
        pipeline = Gst.parse_launch(pipeline_str)
    except Exception as e:
        print(f"    ERROR parseando: {e}")
        continue

    pipeline.set_state(Gst.State.PLAYING)
    pbus = pipeline.get_bus()
    msg = pbus.timed_pop_filtered(5 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)

    if msg is None:
        print(f"    OK: el pipeline '{name}' arrancó.")
        active_pipeline_str = pipeline_str
        break
    else:
        err, debug = msg.parse_error() if msg.type == Gst.MessageType.ERROR else ("EOS", "")
        print(f"    FALLÓ ({msg.type}): {err} - {debug}")
        pipeline.set_state(Gst.State.NULL)
        pipeline = None

if pipeline is None:
    print("\nNo se pudo iniciar ningún pipeline.")
    sys.exit(1)

print(f"\nPublicando a {RTMP_URL} — Ctrl+C para cortar.")
print(f"Consumo en este equipo: http://127.0.0.1:8888/screen/index.m3u8  |  WebRTC: http://127.0.0.1:8889/screen")
print(f"Consumo en la LAN:      http://{LAN_IP}:8888/screen/index.m3u8  |  WebRTC: http://{LAN_IP}:8889/screen\n")

gst_bus = pipeline.get_bus()
main_loop = GLib.MainLoop()


def on_message(bus_, message):
    t = message.type
    if t == Gst.MessageType.EOS:
        print("EOS")
        main_loop.quit()
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print(f"ERROR: {err} - {debug}")
        main_loop.quit()


gst_bus.add_signal_watch()
gst_bus.connect("message", on_message)

try:
    main_loop.run()
except KeyboardInterrupt:
    print("\nCortando...")

pipeline.set_state(Gst.State.NULL)
