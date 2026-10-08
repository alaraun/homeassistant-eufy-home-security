# Network and security

## LAN

- Home Assistant must be on the **same L2 network segment** as every HomeBase. It
  finds stations by UDP broadcast to port 32108 and opens UDP sessions to them; this
  does not work across a router or a VPN.
- **Home Assistant Container**: use `network_mode: host`. A bridge network does not
  see the LAN broadcast.
- Give every HomeBase and standalone camera a **fixed IP** (a DHCP reservation on the
  router).

## Firewall

| Direction | Traffic | Needed for |
|---|---|---|
| Home Assistant → station | UDP 32108, then UDP to the station | discovery, every local command, stills, live video |
| station → Home Assistant | UDP from a random high port, new every session | replies on the same session |
| Home Assistant → internet | HTTPS to `*.eufy.com`, `security-app.eufylife.com` and `security-app-eu.eufylife.com` | sign-in, device list, device keys, session check |
| Home Assistant → internet | UDP 32100 to eufy's rendezvous servers | waking a standalone battery camera |
| Home Assistant → internet | HTTPS to `*.google.com`, `*.googleapis.com` and `app-push-*.eufy.com`; TCP 5228 to `mtalk.google.com` | [Cloud push](usage.md#cloud-push) only |

A host firewall that filters inbound UDP must allow **all UDP from each station's
address**: the station's source port cannot be predicted.

## Reverse proxy

Settings for a proxy (nginx, Caddy, Traefik) in front of Home Assistant:

- Home Assistant: **Settings → System → Network**, enable *Use X-Forwarded-For* and
  add the proxy's address to *Trusted proxies*.
- `/api/websocket`: WebSocket upgrade, read and send timeouts above 60 s (Home
  Assistant pings every 55 s).
- `/api/hls/`: read timeout of at least 65 s. A cold HLS start answers its first
  playlist after about 11 s; a 10 s timeout ends it with a 504 and no picture.

nginx:

```nginx
location /api/websocket {
    proxy_pass http://homeassistant:8123;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 1h;
    proxy_send_timeout 1h;
}

location /api/hls/ {
    proxy_pass http://homeassistant:8123;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 65s;
}
```

## Browsers

| Browser | Live video |
|---|---|
| Chrome, Edge, Safari | WebRTC (HEVC), first picture in about 5 s |
| Firefox | no HEVC over WebRTC, so no picture there; HLS works, about 11 s to the first picture on a cold start. The bundled [camera card](card.md) switches to HLS by itself |

## eufy account

- The account must see the HomeBase: the owner's, or one the owner shared it with
  in the eufy app. A shared home shows only after the invitation is accepted in the
  eufy app, signed in with that account; until then the repair **eufy invitation not
  accepted** names it.
- A sign-in from another client ends the earlier session of the same account. Give
  Home Assistant its own account, shared from the owner's app; share devices added
  later again.
- eufy allows only a few sign-ins per account per day and locks the account for
  24 hours after repeated failures. Home Assistant signs in only at setup and on
  **Reconfigure**.
- An account with two-step verification asks for a code at each new sign-in. eufy
  sends it by e-mail, and the setup, re-authentication and **Reconfigure** dialogs ask
  for it after the password. The session is saved; a restart signs in again only
  after eufy ended it.

## eufy sign-in country

- eufy lists a device only to a sign-in with the country the device was set up or
  shared under. With another country the account signs in fine but lists nothing.
- Home Assistant signs in with the integration's **eufy sign-in country** option,
  Home Assistant's own country by default (**Settings → System → General**). Set it to
  the country your eufy app signs in with (its sign-in screen shows it).
- Changing the option reloads the integration, signs in once more with the new
  country and asks eufy once for the devices.
- Every request also carries Home Assistant's time zone, as the eufy app sends the
  phone's.

## eufy regions

- eufy runs two clouds, `eu` and `us`. Both accept the account's sign-in, but each
  lists only the devices homed on it.
- Adding the account signs in to both and asks both for devices. Each device then
  uses the region that listed it.
- A region that listed no devices is not asked again, so it costs no sign-in. A
  device homed on that region later stays hidden until the option **Look for devices
  in every eufy region** is on and **Refresh device list** is pressed.
- With the option on, every device list Home Assistant fetches asks both regions:
  **Refresh device list**, the session check, and the hourly refresh while a camera
  without a HomeBase is set up. A region whose session has run out costs a sign-in.
- When no region lists any device, the repair issue **No eufy devices found** shows;
  see [troubleshooting.md](troubleshooting.md#repair-issues).

## HomeBase sessions

A HomeBase holds about 9 P2P sessions for all clients together: the eufy app, Home
Assistant, and any other client such as
[eufy-security-ws](https://github.com/bropat/eufy-security-ws). Past that it drops
one. **Sessions per HomeBase** (see [usage](usage.md#options)) sets Home Assistant's
share; two Home Assistant instances on one HomeBase must share it, so give a test
instance 2.

## What is stored and where

| Data | Location | Removed |
|---|---|---|
| eufy session, password, device keys, cloud push registration | Home Assistant's `.storage` | on removal of the integration (the sign-in hold-off is kept) |
| Last image per camera and preset | `<config>/.cache/eufy_home_security/` (not in backups) | on removal |
| Event history images and videos | `<media>/eufy_home_security/` | by the retention option; kept on removal |
| Which recordings were copied (event videos and the card's Station list), and when event videos were switched on | Home Assistant's `.storage` | by the retention option; on removal |

Event history images and videos are visible to everyone who can open Home Assistant's
media browser.

## Stream access

The integration serves each camera's raw stream to Home Assistant's own stream
components only: requests must come from the Home Assistant host itself, carry a
per-start random key, and carry no proxy header. Anything else gets 403. Watch
cameras through Home Assistant's camera views and cards.

Those components read the stream from `127.0.0.1` on Home Assistant's own port:

- **TLS on that port** (`http: ssl_certificate`) is supported. The stream URL is then
  `https`, and its certificate is not checked on this local hop: it names your
  domain, not 127.0.0.1.
- **`http: server_host` bound only to a LAN address** is not supported for live
  view: nothing then answers on 127.0.0.1. Leave `server_host` unset (all addresses)
  or include `127.0.0.1` in it.
