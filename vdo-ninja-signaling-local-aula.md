# VDO.Ninja — Signaling local para el servidor del aula

## Objetivo

Tener VDO.Ninja funcionando **sin dependencia de internet**, dentro de la LAN del aula, para poder transmitir pantalla (instructor editando código en vivo) a las laptops de los alumnos conectados a la misma red. El aula no tiene salida a internet estable, así que el signaling (handshake WebRTC) tiene que resolverse localmente.

Este documento es autocontenido para poder pegarlo en el repo/proyecto del servidor del aula.

## Contexto (por qué esta solución)

- El servidor del aula es una máquina distinta al servidor de la oficina (Forgejo/MinIO/etc.) — no se comparte infraestructura entre ambos.
- VDO.Ninja funciona 100% en navegador (WebRTC), sin cliente que instalar en las laptops de los alumnos.
- El sitio público (vdo.ninja) usa un servidor de signaling en internet (`wss.vdo.ninja`) — como no hay internet garantizado en el aula, hace falta un signaling propio, corriendo en la LAN.
- Repo oficial pensado exactamente para esto: [`steveseguin/offline_deployment`](https://github.com/steveseguin/offline_deployment). Combina servidor web + servidor de signaling (websocket) en un solo proceso Node.js.

## Arquitectura resultante

```
[Laptop instructor] --(broadcast/source)--> [Servidor aula: vdo.ninja + signaling]
[Laptops alumnos]   <--(viewer)------------ [Servidor aula: vdo.ninja + signaling]
```

Todo el tráfico de video queda dentro de la LAN. No sale a internet en ningún punto.

## Requisitos previos en el servidor del aula

- Node.js + npm (o correrlo containerizado — ver opción Docker más abajo).
- Puerto disponible para HTTPS (443 por defecto, o cualquier otro vía `PORT`).
- `openssl` para generar certificado autofirmado.

## Instalación nativa (paso a paso)

```bash
# 1. Clonar el frontend de VDO.Ninja
git clone https://github.com/steveseguin/vdo.ninja

# 2. Clonar el servidor de signaling + webserver
git clone https://github.com/steveseguin/offline_deployment webserver

# 3. Apuntar el frontend al signaling LOCAL en vez del público (wss.vdo.ninja)
sed -i 's/\/\/ session\.customWSS = true;/session.wss = "wss:\/\/"+window.location.host;session.customWSS = true;session.salt = "vdo.ninja";session.configuration = {};/' ./vdo.ninja/index.html

# 4. Instalar dependencias del servidor
cd webserver
npm install
npm install express

# 5. Generar certificado autofirmado (WebRTC exige HTTPS incluso en LAN)
openssl req -nodes -new -x509 -keyout key.pem -out cert.pem
cp cert.pem ../vdo.ninja/cert.pem

# 6. Levantar el servidor
sudo nodejs server.js
# o, para que arranque solo en cada boot:
sudo cp vdoninja.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable vdoninja
sudo systemctl restart vdoninja
```

Verificar la IP del servidor con `ifconfig` — el sitio queda disponible en `https://<ip-servidor-aula>/`.

## Opción Docker (alternativa)

El repo trae también una imagen Docker, si se prefiere containerizar (más parecido al patrón de trabajo habitual con Podman quadlets, aunque acá es un servidor nuevo/independiente del de la oficina):

```bash
docker run \
  --mount type=bind,source="$(pwd)"/certs,target=/var/certs \
  -e KEY_PATH=/var/certs/private.key \
  -e CERT_PATH=/var/certs/certificate.crt \
  -p 8443:8443 \
  vdoninja
```

## El punto delicado: certificados autofirmados

Sin CA real disponible offline, el navegador va a marcar el sitio como "no seguro":

- **Instructor (source, el que comparte pantalla):** necesita aceptar/confiar el certificado correctamente — `getDisplayMedia` puede ser más estricto en contexto no seguro.
- **Alumnos (viewers, solo miran):** alcanza con click en "Avanzado → Continuar" la primera vez que abren el link. No requiere instalar el cert en el sistema.
- Si en algún punto se quiere eliminar del todo la advertencia, el cert (`cert.pem`) queda descargable desde `https://<ip-servidor-aula>/cert.pem` para instalarlo manualmente donde haga falta.

## Flujo de uso en clase

1. Instructor abre `https://<ip-servidor-aula>/` como *source*, comparte su pantalla (donde está editando el código del alumno).
2. Alumnos abren el link de *viewer* correspondiente en una pestaña — sin instalar nada.
3. Nada de esto depende de internet: todo el signaling y el video quedan dentro de la LAN del aula.

## Pendiente / a decidir

- [ ] Definir si el servidor del aula tiene IP fija dentro de esa LAN o hay que resolverlo por DHCP cada vez (afecta si conviene fijar un hostname local en vez de IP).
- [ ] Confirmar puerto a usar (443 vs. alternativo tipo 8443) según qué más corra en ese servidor.
- [ ] Evaluar si además de VDO.Ninja este servidor del aula debe alojar TLJH — o si TLJH sigue en otra máquina.
- [ ] **Importante:** el flujo de "tocar el código del alumno" que se pensó en paralelo (push/pull vía Forgejo) depende del Forgejo de la oficina, que está detrás de Cloudflare Tunnel — es decir, requiere internet. Si el aula no tiene internet estable, ese paso de la integración no funciona tal cual y hay que resolverlo aparte (¿Forgejo espejado localmente? ¿otro mecanismo de intercambio de archivos dentro de la LAN del aula?).
