"""Every camera's live video, as MPEG-TS over Home Assistant's own HTTP server.

The library opens the camera, muxes the transport stream with the camera's AAC, fans
one camera stream out to every viewer and handles slow readers. This module is the
Home Assistant glue: one broadcast per camera, one HTTP view that serves them, and the
arbitration that makes a live view release the station session's media slot.

- **Nothing here decodes or re-encodes video.** The library's chunks are written
  through untouched: decoding one 4K HEVC stream on a four-core board runs at 0.58x
  realtime, remuxing it costs about 2% of one core.
- **A camera opens only on a real HTTP GET.** A broadcast is built per streamable
  camera at setup and costs nothing (no session, wake or task) until its first
  subscriber. Nothing polls or pre-warms, and an ended stream is not reopened here.
- **Only Home Assistant's own consumers may read a stream.** The ``stream`` component
  and go2rtc dial the URL with no HA credentials, so the view cannot use HA's auth. It
  serves a request only when the socket peer is loopback, no proxy forwarding header
  is present, and the query carries this run's random secret
  (:data:`STREAM_AUTH_PARAM`, compared in constant time); anything else is a 403 that
  opens nothing. The serial in the path is a scoped exception to the redaction rule
  (see ``const.STREAM_URL_PATH``); log lines still redact, and neither the URL nor the
  secret is logged.
- **Two viewers of one camera cost one camera open.** The cap on live streams per
  station is the library's; none is added here.
- **Audio is always carried.** The AAC track is declared at stream start and silent
  until the camera's first audio frame. Home Assistant's go2rtc adds its Opus
  transcode, audio only, when a WebRTC viewer connects.
- **A live view yields to a still only past the session budget.** On a HomeBase a live
  still or a preset capture runs on an extra session beside the views; past the
  budget (the sessions per HomeBase option) the broadcast of the camera holding the
  station session's slot (``Station.media_slot_camera``) ends and the capture takes the
  freed slot (``snapshots.async_open_media``). A standalone camera has one stream, so
  a capture ends its live view first. The viewer sees a normal end of stream and can
  retry; a detection still cannot be taken again.

The view is registered once per Home Assistant instance, because ``register_view`` has
no unregister; it holds no per-entry state and looks the broadcast up at request time,
so a serial whose entry has unloaded is a clean 404.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from aiohttp import ClientConnectionResetError, web
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.http import HomeAssistantView
from homeassistant.util.hass_dict import HassKey

from eufy_home_security import (
    MIN_ZOOM,
    CameraWakeError,
    EufySecurityError,
    FrameStream,
    LiveStreamLimitError,
    Station,
    StreamBroadcast,
    redact_serial,
)

from . import detections, errors
from .const import (
    DOMAIN,
    STREAM_AUTH_PARAM,
    STREAM_CONTENT_TYPE,
    STREAM_FORWARDING_HEADERS,
    STREAM_URL_PATH,
    STREAM_VIEW_NAME,
    STREAM_YIELD_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

STREAMS: HassKey[StreamRegistry] = HassKey(f"{DOMAIN}_streams")


def make_broadcast(
    station: Station, device_sn: str, open_stream: Callable[[], Awaitable[FrameStream]]
) -> StreamBroadcast:
    """One shared live stream for one camera; opens nothing until someone subscribes.

    ``open_stream`` (``StreamManager._async_open``) opens the camera at its live-view
    preset and zoom on each subscribe after the stream has ended. Module level so a test
    can substitute it. The stream starts at the first keyframe and carries picture-size
    changes (the opening ramp, a zoom) as resizes. ``audio=True`` always: the program
    map is written once at stream start, so the AAC track is declared up front and is
    silent without camera audio. ``standalone`` lets the library pick per-model timing.
    """
    return StreamBroadcast(
        open_stream,
        audio=True,
        standalone=station.is_standalone,
        name=redact_serial(device_sn),
    )


@dataclass(slots=True)
class StreamRegistry:
    """The live broadcasts of this Home Assistant instance, by camera serial.

    Instance-wide rather than per entry, because the view that reads it can never be
    unregistered. A camera serial is globally unique, so several accounts share one
    registry safely. Adding and removing is the per-entry manager's; it drops its
    serials at unload, so a request afterwards gets a clean 404.
    """

    broadcasts: dict[str, StreamBroadcast] = field(default_factory=dict)
    view_registered: bool = False
    # The stream URL's secret: random per Home Assistant run, never stored or logged.
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))


def stream_registry(hass: HomeAssistant) -> StreamRegistry:
    """The one stream registry of this Home Assistant instance, created on first use."""
    registry = hass.data.get(STREAMS)
    if registry is None:
        registry = StreamRegistry()
        hass.data[STREAMS] = registry
    return registry


class EufyStreamView(HomeAssistantView):
    """Serves one camera's muxed MPEG-TS to whoever connects, for as long as they read."""

    url = STREAM_URL_PATH
    name = STREAM_VIEW_NAME
    # go2rtc and the stream component dial this URL with no HA credentials, so HA's
    # auth would lock both out; _refusal() guards it: loopback peer, no forwarding
    # header, URL secret.
    requires_auth = False

    def __init__(self, registry: StreamRegistry) -> None:
        """Hold the instance-wide registry; this view owns no per-entry state."""
        self._registry = registry

    def _refusal(self, request: web.Request) -> str | None:
        """Why ``request`` may not read a stream; None when it may.

        The peer is read from the socket, not ``request.remote``, which HA's forwarded
        middleware rewrites from ``X-Forwarded-For``. A request carrying any forwarding
        header came through a proxy, whatever its socket peer, and is refused.
        """
        if any(header in request.headers for header in STREAM_FORWARDING_HEADERS):
            return "forwarded by a proxy"
        transport = request.transport
        peer = transport.get_extra_info("peername") if transport is not None else None
        try:
            address = ipaddress.ip_address(peer[0]) if peer else None
        except ValueError:
            address = None
        if address is None or not address.is_loopback:
            return "not from this host"
        offered = request.query.get(STREAM_AUTH_PARAM, "")
        if not hmac.compare_digest(offered.encode(), self._registry.secret.encode()):
            return "missing or wrong stream secret"
        return None

    async def get(self, request: web.Request, device_sn: str) -> web.StreamResponse:
        """Subscribe to the camera's broadcast and write its chunks until it ends.

        Leaving the library generator (stream end, client gone, broadcast closed) runs
        its cleanup, which releases the camera after the last viewer. A later joiner
        gets the header chunk first, so it can decode at once.

        The status is sent with the first chunk. A HomeBase camera the station could not
        wake (``CameraWakeError``, also raised at once during the library's wake backoff)
        and an open past the session budget (``LiveStreamLimitError``, once its wait for
        a session runs out) are a 503; any other end without data is an empty 200. Home
        Assistant's stream worker waits 30 s for the first data.
        """
        serial = redact_serial(device_sn)
        if (refusal := self._refusal(request)) is not None:
            # Checked before the registry, so a refusal says nothing about the serial.
            _LOGGER.debug("Live stream of %s: request refused (%s), 403", serial, refusal)
            return web.Response(status=HTTPStatus.FORBIDDEN)
        broadcast = self._registry.broadcasts.get(device_sn)
        if broadcast is None:
            # An unknown camera, or one whose config entry has unloaded. Nothing is
            # opened and no camera is woken.
            _LOGGER.debug("Live stream of %s: no broadcast registered, 404", serial)
            return web.Response(status=HTTPStatus.NOT_FOUND)
        _LOGGER.debug(
            "Live stream of %s: a viewer connected (%d already attached)",
            serial,
            broadcast.subscribers,
        )
        response = web.StreamResponse(headers={"Content-Type": STREAM_CONTENT_TYPE})
        chunks = 0
        # The status waits for the first chunk: a camera the station could not wake ends
        # the subscription with no data, and gets a 503 instead of an empty 200. A cancel
        # propagates; leaving the block unsubscribes.
        async with contextlib.aclosing(broadcast.subscribe()) as stream:
            first = await anext(stream, None)
            error = broadcast.error
            if first is None and isinstance(error, CameraWakeError):
                retry = error.retry_after
                _LOGGER.debug(
                    "Live stream of %s: the camera did not wake%s; 503",
                    serial,
                    f", next attempt in {retry:.0f} s" if retry else "",
                )
                return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
            if first is None and isinstance(error, LiveStreamLimitError):
                _LOGGER.info(
                    "Live stream of %s: refused, the sessions per HomeBase option allows "
                    "%d live stream(s) and none closed in time; 503",
                    serial,
                    error.limit,
                )
                return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)
            await response.prepare(request)
            try:
                if first is not None:
                    await response.write(first)
                    chunks += 1
                    async for chunk in stream:
                        await response.write(chunk)
                        chunks += 1
            except ConnectionResetError, ClientConnectionResetError:
                # The viewer went away mid-write. Not an error, and not this module's to
                # report: leaving the loop has already unsubscribed.
                pass
        # Read promptly: the library clears this when a new stream starts.
        error = broadcast.error
        # Nothing is reopened here: Home Assistant asks for the URL again by itself.
        # The error type, never the library's message, plus the eufy-session clause when
        # a replaced session is why the camera could not be reached.
        _LOGGER.debug(
            "Live stream of %s ended after %d chunk(s): %s",
            serial,
            chunks,
            "clean end (viewer left or stream closed)"
            if error is None
            else errors.failure_reason(error),
        )
        return response


@callback
def async_register_view(hass: HomeAssistant) -> None:
    """Register the stream view, once per Home Assistant instance.

    ``hass.http.register_view`` has no unregister counterpart, and several config
    entries (one per eufy account) are a supported shape, so a second entry must not
    add a duplicate route. Guarded by a flag on the instance-wide registry, in the
    shape ``runtime.station_claims`` uses.
    """
    registry = stream_registry(hass)
    if registry.view_registered:
        return
    registry.view_registered = True
    hass.http.register_view(EufyStreamView(registry))
    _LOGGER.debug("The live stream view is registered for this Home Assistant instance")


def stream_url(hass: HomeAssistant, device_sn: str) -> str:
    """The camera's live stream URL: a pure string build, no I/O.

    Home Assistant asks for it on every WebRTC offer and every recording, under a
    ten-second bound. Loopback, on the port Home Assistant's HTTP server uses, with the
    run's secret under ``auth``, a key HA's stream component redacts when it logs a
    source URL. ``https`` when that server has a certificate, since its one port then
    speaks only TLS; the certificate names another host, so the reader must not verify
    it (go2rtc skips verification for an IP host, the stream worker is told so by
    ``camera.EufyCamera.async_create_stream``).
    """
    api = hass.config.api
    scheme = "https" if api is not None and api.use_ssl else "http"
    query = urlencode({STREAM_AUTH_PARAM: stream_registry(hass).secret})
    path = STREAM_URL_PATH.format(device_sn=device_sn)
    return f"{scheme}://127.0.0.1:{hass.http.server_port}{path}?{query}"


class StreamManager:
    """One entry's live broadcasts: built at setup, opened only by a viewer."""

    def __init__(self, hass: HomeAssistant, entry: EufyConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._registry = stream_registry(hass)
        # Every serial this entry put in the instance-wide registry, and its station.
        self._serials: set[str] = set()
        self._station_objs: dict[str, Station] = {}
        # Per pan/tilt camera, the preset its live view opens at; absent = camera default.
        self._live_presets: dict[str, int] = {}
        # Per zoom camera, the live view's picture zoom; absent = the slot's own zoom.
        self._live_zooms: dict[str, float] = {}
        # Cameras whose live view is opening: their zoom reports are the camera's reset.
        self._opening: set[str] = set()
        self._stopped = False
        _LOGGER.debug(
            "Stream manager started: a camera opens only when a viewer connects, and "
            "its audio is carried whenever it sends any"
        )

    @callback
    def async_add_camera(self, station: Station, device_sn: str) -> None:
        """Give a camera a broadcast and register it under its serial; opens nothing.

        A ``StreamBroadcast`` costs no session, wake or task until a subscriber comes,
        so ``stream_source()`` stays cheap and its URL stable.
        """
        if self._stopped:
            return
        self._registry.broadcasts[device_sn] = make_broadcast(
            station, device_sn, lambda: self._async_open(station, device_sn)
        )
        self._station_objs[device_sn] = station
        self._serials.add(device_sn)
        _LOGGER.debug(
            "Live stream ready for %s; nothing is open until a viewer connects",
            redact_serial(device_sn),
        )

    def has_camera(self, device_sn: str) -> bool:
        """Whether ``device_sn`` has a live broadcast of this entry."""
        return device_sn in self._serials

    def broadcast(self, device_sn: str) -> StreamBroadcast | None:
        """``device_sn``'s live broadcast, for a capture beside the views; None without one."""
        if device_sn not in self._serials:
            return None
        return self._registry.broadcasts.get(device_sn)

    def live_preset(self, device_sn: str) -> int | None:
        """The slot ``device_sn``'s next live view opens at; None for the camera default.

        A chosen slot the last read shows empty yields None, so a stale choice opens at
        the default rather than failing the view. Before any read the choice stands and
        the camera decides.
        """
        index = self._live_presets.get(device_sn)
        station = self._station_objs.get(device_sn)
        if index is None or station is None:
            return None
        slots = station.presets(device_sn)
        if slots is not None and not any(s.index == index and s.enabled for s in slots):
            return None
        return index

    def chosen_live_preset(self, device_sn: str) -> int | None:
        """The stored choice for ``device_sn``, unchecked against the slots."""
        return self._live_presets.get(device_sn)

    @callback
    def async_set_live_preset(self, device_sn: str, index: int | None) -> None:
        """Store the preset ``device_sn``'s live views open at; sends nothing."""
        if index is None:
            self._live_presets.pop(device_sn, None)
        else:
            self._live_presets[device_sn] = index

    @callback
    def async_choose_live_preset(self, device_sn: str, index: int | None) -> None:
        """Store a chosen live-view preset, and take that slot's stored zoom with it.

        The camera applies a slot's zoom itself on every turn, so the live zoom follows.
        """
        self.async_set_live_preset(device_sn, index)
        self._async_zoom_to(device_sn, self._slot_zoom(device_sn, self.live_preset(device_sn)))

    def live_zoom(self, device_sn: str) -> float:
        """The picture zoom ``device_sn``'s live view shows, or opens at (1.0 = 1x)."""
        return self._live_zooms.get(
            device_sn, self._slot_zoom(device_sn, self.live_preset(device_sn))
        )

    @callback
    def async_restore_live_zoom(self, device_sn: str, zoom: float) -> None:
        """Store a restored live-view zoom; sends nothing and tells no entity."""
        self._live_zooms[device_sn] = zoom

    async def async_set_live_zoom(self, device_sn: str, zoom: float) -> bool:
        """Set ``device_sn``'s live-view zoom; with a view running, zoom the camera first.

        Returns whether a running view was zoomed. Nothing is stored when the camera
        write fails; library errors propagate to the caller. A zoom that changes the
        picture size keeps the view running: the broadcast follows the new size.
        """
        station = self._station_objs.get(device_sn)
        running = station is not None and self._running(device_sn)
        if station is not None and running:
            await station.async_set_zoom(device_sn, zoom)
        self._async_zoom_to(device_sn, zoom)
        return running

    async def async_goto_preset(self, device_sn: str, index: int) -> bool:
        """Turn ``device_sn`` to slot ``index`` once, returning at the camera's receipt.

        The live-view choices stay; during a running view the live zoom shows the
        slot's own zoom, which the camera applies with the turn. Returns whether a view
        was running. Library errors propagate to the caller.
        """
        station = self._station_objs.get(device_sn)
        if station is None:
            return False
        await station.async_goto_preset(device_sn, index, settle=0)
        if not self._running(device_sn):
            return False
        self._async_zoom_to(device_sn, self._slot_zoom(device_sn, index))
        return True

    @callback
    def async_apply_zoom_report(self, device_sn: str, zoom: float) -> None:
        """The camera reported ``zoom`` (``ZoomChanged``): show it while a view runs.

        With no view running, or while one opens, the report is the camera's own reset
        (idle, wake, or the open's slot zoom), not a choice: the stored live-view zoom
        stays, and the open re-applies it.
        """
        if (
            device_sn not in self._serials
            or device_sn in self._opening
            or not self._running(device_sn)
        ):
            return
        if self._live_zooms.get(device_sn) != zoom:
            _LOGGER.debug(
                "Live stream of %s: the camera reports zoom %g", redact_serial(device_sn), zoom
            )
            self._async_zoom_to(device_sn, zoom)

    def _running(self, device_sn: str) -> bool:
        broadcast = self._registry.broadcasts.get(device_sn)
        return broadcast is not None and broadcast.running

    def _slot_zoom(self, device_sn: str, index: int | None) -> float:
        """The zoom stored with slot ``index`` (None: the default slot); 1.0 if unknown."""
        station = self._station_objs.get(device_sn)
        if station is None:
            return MIN_ZOOM
        if index is None:
            index = station.default_preset(device_sn)
        slot = next(
            (s for s in station.presets(device_sn) or () if s.index == index and s.enabled),
            None,
        )
        return float(slot.zoom) if slot is not None and slot.zoom >= MIN_ZOOM else MIN_ZOOM

    @callback
    def _async_zoom_to(self, device_sn: str, zoom: float) -> None:
        self._live_zooms[device_sn] = zoom
        async_dispatcher_send(self._hass, detections.zoom_signal(self._entry.entry_id, device_sn))

    async def _async_open(self, station: Station, device_sn: str) -> FrameStream:
        """Open ``device_sn`` live at its chosen preset, then apply its chosen zoom.

        The zoom is sent right after the open; its size change reaches the viewer as a
        resize of the running stream. A zoom the camera refuses is logged and the view
        carries on at the slot's zoom; the stored zoom stays for the next open. Zoom
        reports during the open are ignored (:meth:`async_apply_zoom_report`), so the
        camera's reset cannot replace the stored zoom before it is sent. ``wait=True``
        lets a view past the session budget wait for a session (up to the library's
        first-frame timeout) rather than fail at once.
        """
        self._opening.add(device_sn)
        try:
            return await self._async_open_zoomed(station, device_sn)
        finally:
            self._opening.discard(device_sn)

    async def _async_open_zoomed(self, station: Station, device_sn: str) -> FrameStream:
        preset = self.live_preset(device_sn)
        stream = await station.async_open_live(device_sn, preset=preset, wait=True)
        zoom = self._live_zooms.get(device_sn)
        if zoom is None or zoom == self._slot_zoom(device_sn, preset):
            return stream
        try:
            await station.async_set_zoom(device_sn, zoom)
        except (EufySecurityError, ValueError) as err:
            _LOGGER.debug(
                "Live stream of %s opened without its zoom: %s",
                redact_serial(device_sn),
                errors.failure_reason(err),
            )
        except asyncio.CancelledError:
            await stream.aclose()
            raise
        else:
            _LOGGER.debug("Live stream of %s opened at zoom %g", redact_serial(device_sn), zoom)
        return stream

    async def async_turn_live_view(self, device_sn: str, index: int | None) -> bool:
        """Turn ``device_sn``'s running live view to slot ``index`` (None: its default).

        Returns False when no view runs or the default is unknown, and nothing is sent.
        The stream keeps running across the turn, a picture-size change included (the
        broadcast follows it). Library errors propagate to the caller.
        """
        broadcast = self._registry.broadcasts.get(device_sn)
        station = self._station_objs.get(device_sn)
        if broadcast is None or station is None or not broadcast.running:
            return False
        target = index if index is not None else station.default_preset(device_sn)
        if target is None:
            return False
        _LOGGER.debug("Live stream of %s: turning to preset %d", redact_serial(device_sn), target)
        await station.async_goto_preset(device_sn, target, settle=0)
        return True

    async def async_yield_media(self, station_serial: str) -> None:
        """End the live view holding the station session's media slot, if any.

        Called by ``snapshots.async_open_media``: on a HomeBase once a capture met the
        session budget, on a standalone camera before every capture. Only the broadcast
        of the camera the library names (``Station.media_slot_camera``) ends; views on
        extra sessions keep running, and nothing ends when the slot is free or holds a
        recording. The holder is read once; a view opened after the read leaves the
        capture's own open to wait or fail as the library decides.

        The viewer sees a normal end of stream, and the broadcast is reused by the next
        subscriber. A cheap no-op when nothing runs; a close that overruns finishes
        detached, because a detection still is time-critical.
        """
        station = next((s for s in self._station_objs.values() if s.serial == station_serial), None)
        holder = None if station is None else station.media_slot_camera
        if holder is None or holder not in self._serials:
            return
        await self._async_abort(holder, "a media operation needs the station's one media slot")

    async def async_restart_camera(self, device_sn: str) -> None:
        """End ``device_sn``'s live view after a setting changed what the camera sends.

        Called for the streaming-quality ladder, which moves resolution and bitrate
        (verified on hardware: a view running across the change froze, and Home
        Assistant's stream worker logged that the packets stopped). The next viewer
        opens fresh; Home Assistant asks for the URL again by itself. A no-op without a
        viewer.
        """
        await self._async_abort(device_sn, "a setting changed the picture the camera sends")

    async def _async_abort(self, device_sn: str, reason: str) -> None:
        """Close ``device_sn``'s broadcast if it is running, never raising.

        A close that overruns finishes in the background instead of holding up the
        caller.
        """
        broadcast = self._registry.broadcasts.get(device_sn)
        if broadcast is None or not broadcast.running:
            return
        serial = redact_serial(device_sn)
        _LOGGER.debug("Live stream of %s: aborting, %s", serial, reason)
        try:
            async with asyncio.timeout(STREAM_YIELD_TIMEOUT_SECONDS):
                await broadcast.aclose()
        except TimeoutError:
            _LOGGER.debug(
                "Live stream of %s: the close is taking longer than %d s; finishing "
                "it in the background so the caller is not held up",
                serial,
                STREAM_YIELD_TIMEOUT_SECONDS,
            )
            self._entry.async_create_background_task(
                self._hass,
                broadcast.aclose(),
                name=f"{DOMAIN} stream close {serial}",
            )

    async def async_stop(self) -> None:
        """Close every broadcast and drop its serial; a request after this is ignored.

        ``aclose()`` is idempotent and safe with viewers still attached: it pushes the
        end sentinel to each of them, which ends the view's ``async for`` and lets its
        response finish, so no HTTP handler is left hanging on a stream that is over.
        Dropping the serials from the instance-wide registry is what makes a request
        after unload a clean 404 rather than a stale broadcast.
        """
        self._stopped = True
        running = sum(
            1
            for device_sn in self._serials
            if (broadcast := self._registry.broadcasts.get(device_sn)) is not None
            and broadcast.running
        )
        _LOGGER.debug("Stream manager stopping: %d live stream(s) running", running)
        for device_sn in self._serials:
            broadcast = self._registry.broadcasts.pop(device_sn, None)
            if broadcast is None:
                continue
            with contextlib.suppress(asyncio.CancelledError):
                await broadcast.aclose()
        self._serials.clear()
        self._station_objs.clear()
