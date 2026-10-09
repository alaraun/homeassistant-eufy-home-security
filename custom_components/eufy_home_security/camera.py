"""One camera entity per camera, showing a still of its own latest detection.

The image is the snapshot manager's (``snapshots.py``): the detection's thumbnail,
upgraded to the event's 4K trigger frame once HA's ffmpeg decoded it, or, with the
live-snapshot option on and no detection image yet, one live keyframe per cooldown.
A view serves what is cached and never waits on the station; a view of a camera
with no image may only schedule the live keyframe in the background.

Live video: a camera the library can open live on its station
(``Station.live_support`` not ``UNKNOWN``) carries ``CameraEntityFeature.STREAM`` and hands Home
Assistant a stable loopback MPEG-TS URL (``streaming.py``). Building that URL opens
nothing — the camera opens only when something actually connects to it — and
snapshots stay on ``async_camera_image``, never taken from the stream. A camera
without live support advertises no stream at all. The entity has no name of its
own, the camera is its device.

The camera entity is also the target of the ``eufy_home_security.capture_preset``
action: with a ``preset`` slot index it turns a pan/tilt
camera to that preset and takes a live image of the view, which lands on that
preset's image entity (``image.py``), never on the camera. The call returns at once;
a camera held by another capture, an unset slot or a model without presets raise a
translated error before anything is sent (``presets.PresetManager``).

Pan/tilt and zoom for camera cards: Home Assistant's camera entity has no PTZ feature,
so the camera entity carries three actions a card can call, as ONVIF's ``ptz`` does:
``pan_tilt`` (one step, as the step buttons), ``goto_preset`` (a one-off turn; the
live-view preset select is left as it is) and ``zoom`` (the live-view zoom one step in
or out, as the zoom number). A model without pan/tilt control refuses all three with
``pan_tilt_unsupported``, and ``zoom`` also needs the zoom capability (``ptz.py``).
Two more edit the camera's slots: ``save_preset`` stores the current view (the lowest
free slot, or ``preset`` to overwrite one; ``make_default`` also makes it the default)
and returns ``{"preset": index}`` when asked, and ``delete_preset`` clears a slot. Both
need pan/tilt control and presets.

``record`` saves a clip of the camera's live broadcast as an MP4 in the event history
(``history.py``, kind ``live``): ``duration`` seconds, else the Recording length option.
It shares a running live view, wakes a battery camera otherwise, and shows the camera
as recording while it runs; one clip per camera at a time. It responds with the file's
``media_content_id``, the clip's ``duration`` and whether it is ``complete``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, override

import voluptuous as vol
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from eufy_home_security import (
    MAX_ZOOM,
    MIN_ZOOM,
    ClipWriter,
    EufySecurityError,
    MediaClip,
    PanTilt,
    redact_serial,
)

from . import (
    detections,
    errors,
    history,
    presets,
    ptz,
    runtime,
    small_images,
    snapshots,
    streaming,
)
from .const import (
    ATTR_COMPLETE,
    ATTR_DIRECTION,
    ATTR_DURATION,
    ATTR_IMAGE_SOURCE,
    ATTR_IMAGE_UPDATED,
    ATTR_MAKE_DEFAULT,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_PRESET,
    ATTR_TRIGGERED_AT,
    CAMERA_FRAME_INTERVAL_SECONDS,
    CAMERA_KEY,
    CLIP_REMUX_TIMEOUT_SECONDS,
    CONF_RECORD_LENGTH,
    DEFAULT_RECORD_LENGTH_SECONDS,
    MAX_RECORD_LENGTH_SECONDS,
    MIN_RECORD_LENGTH_SECONDS,
    PRESET_MAX_INDEX,
    SERVICE_CAPTURE_PRESET,
    SERVICE_DELETE_PRESET,
    SERVICE_GOTO_PRESET,
    SERVICE_PAN_TILT,
    SERVICE_RECORD,
    SERVICE_SAVE_PRESET,
    SERVICE_ZOOM,
    ZOOM_ACTION_STEP,
    ZOOM_IN,
    ZOOM_OUT,
)
from .coordinator import StationCoordinator
from .entity import EufyDeviceEntity, EufyPushAvailability
from .presets import PresetManager
from .snapshots import SnapshotManager

if TYPE_CHECKING:
    from homeassistant.components.stream import Stream

    from .runtime import EufyConfigEntry

_LOGGER = logging.getLogger(__name__)

# Images come from the manager's cache, so HA need not serialise entity updates.
PARALLEL_UPDATES = 0
# The history kind of a recorded live clip.
_LIVE_KIND = "live"


def _record_length(options: Mapping[str, Any]) -> int:
    """The Recording length option; the default for an unset or out-of-range value."""
    value = options.get(CONF_RECORD_LENGTH)
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_RECORD_LENGTH_SECONDS <= value <= MAX_RECORD_LENGTH_SECONDS
    ):
        return value
    return DEFAULT_RECORD_LENGTH_SECONDS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a camera entity per paired device with detection entities; register its actions."""
    del hass  # the runtime data carries everything this platform needs
    manager = entry.runtime_data.snapshots
    presets = entry.runtime_data.presets
    async_add_entities(
        EufyCamera(coordinator, device_sn, manager, presets)
        for coordinator in entry.runtime_data.coordinators.values()
        for device_sn, kind in detections.paired_device_kinds(coordinator.station).items()
        if detections.has_detection_entities(kind)
    )
    # Once per platform load; each schema is the user input's only validation before
    # the manager's own checks.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_CAPTURE_PRESET,
        {vol.Required(ATTR_PRESET): _PRESET_INDEX},
        "async_capture_preset",
    )
    platform.async_register_entity_service(
        SERVICE_PAN_TILT,
        {vol.Required(ATTR_DIRECTION): vol.In(list(_DIRECTIONS))},
        "async_pan_tilt",
    )
    platform.async_register_entity_service(
        SERVICE_GOTO_PRESET,
        {vol.Required(ATTR_PRESET): _PRESET_INDEX},
        "async_goto_preset",
    )
    platform.async_register_entity_service(
        SERVICE_ZOOM,
        {vol.Required(ATTR_DIRECTION): vol.In([ZOOM_IN, ZOOM_OUT])},
        "async_zoom",
    )
    platform.async_register_entity_service(
        SERVICE_SAVE_PRESET,
        {
            vol.Optional(ATTR_PRESET): _PRESET_INDEX,
            vol.Optional(ATTR_MAKE_DEFAULT, default=False): cv.boolean,
        },
        "async_save_preset",
        supports_response=SupportsResponse.OPTIONAL,
    )
    platform.async_register_entity_service(
        SERVICE_DELETE_PRESET,
        {vol.Required(ATTR_PRESET): _PRESET_INDEX},
        "async_delete_preset",
    )
    platform.async_register_entity_service(
        SERVICE_RECORD,
        {vol.Optional(ATTR_DURATION): _RECORD_SECONDS},
        "async_record",
        supports_response=SupportsResponse.OPTIONAL,
    )


# A slot index as every preset action takes it.
_PRESET_INDEX = vol.All(vol.Coerce(int), vol.Range(min=0, max=PRESET_MAX_INDEX))
# The record action's duration in seconds.
_RECORD_SECONDS = vol.All(
    vol.Coerce(int), vol.Range(min=MIN_RECORD_LENGTH_SECONDS, max=MAX_RECORD_LENGTH_SECONDS)
)
# What a capture may take beyond its duration: the library's start bound and the remux.
_RECORD_MARGIN_SECONDS = 60 + CLIP_REMUX_TIMEOUT_SECONDS

# The pan_tilt action's direction values.
_DIRECTIONS: dict[str, PanTilt] = {
    "left": PanTilt.LEFT,
    "right": PanTilt.RIGHT,
    "up": PanTilt.UP,
    "down": PanTilt.DOWN,
}


class EufyCamera(EufyPushAvailability, EufyDeviceEntity, small_images.SmallImageSource, Camera):
    """A camera's latest detection still; available while its station's session is up."""

    _attr_name = None
    _attr_brand = "eufy"
    _attr_frame_interval = CAMERA_FRAME_INTERVAL_SECONDS
    # A new value per still: history would store one attributes row per image.
    _unrecorded_attributes = frozenset({ATTR_IMAGE_UPDATED})

    def __init__(
        self,
        coordinator: StationCoordinator,
        device_sn: str,
        manager: SnapshotManager,
        presets: PresetManager,
    ) -> None:
        super().__init__(coordinator, device_sn, CAMERA_KEY)
        Camera.__init__(self)
        self._serial = device_sn
        self._manager = manager
        self._presets = presets
        # Per instance, never at class scope: only a camera the library can open live
        # on its station advertises a stream, so Home Assistant never offers a live
        # view the library would refuse to open.
        if detections.has_live_stream(coordinator.station, device_sn):
            self._attr_supported_features = CameraEntityFeature.STREAM

    @override
    async def stream_source(self) -> str | None:
        """The camera's live MPEG-TS URL, or None when this model has no live stream.

        A single string build: no I/O, no await of the station, no registry read. Home
        Assistant bounds this at ten seconds and asks for it on every WebRTC offer and
        when the entity is added, so it must be cheap and stable. Constructing the URL
        opens nothing — the camera opens only when something connects to it.
        """
        if not self.supported_features & CameraEntityFeature.STREAM:
            return None
        return streaming.stream_url(self.hass, self._serial)

    @override
    async def async_create_stream(self) -> Stream | None:
        """Home Assistant's stream of this camera, told not to verify a TLS certificate.

        Over ``https`` the certificate names Home Assistant's host, never 127.0.0.1, and
        FFmpeg 9 verifies by default. ``stream_options`` cannot carry the flag: the
        stream component's schema refuses it.
        """
        stream = await super().async_create_stream()
        if stream is not None and stream.source.startswith("https:"):
            stream.pyav_options.setdefault("tls_verify", "0")
        return stream

    async def async_capture_preset(self, preset: int) -> None:
        """The ``capture_preset`` action: start the preset capture and return at once.

        The same path as the slot's "Capture preset n" button. A model without presets
        raises ``presets_unsupported``, an unset slot ``preset_not_set``, a camera held
        by another capture ``capture_in_progress``; nothing is sent in any of them.
        """
        self._presets.async_request_preset(self.coordinator.station, self._serial, preset)

    async def async_pan_tilt(self, direction: str) -> None:
        """The ``pan_tilt`` action: one step, as the step buttons."""
        if not detections.has_pan_tilt_control(self._serial):
            raise errors.pan_tilt_unsupported()
        await ptz.async_pan_tilt(
            self.coordinator, self._serial, _DIRECTIONS[direction], SERVICE_PAN_TILT
        )

    async def async_goto_preset(self, preset: int) -> None:
        """The ``goto_preset`` action: turn to slot ``preset`` once, at the receipt.

        A model without presets raises ``presets_unsupported`` and a slot the last read
        showed unset ``preset_not_set``, both before sending.
        """
        self._require_presets()
        station = self.coordinator.station
        if station.presets(self._serial) is not None and preset not in presets.enabled_indexes(
            station, self._serial
        ):
            raise errors.preset_not_set(preset)
        await ptz.async_goto_preset(self.coordinator, self._serial, preset, SERVICE_GOTO_PRESET)

    async def async_save_preset(
        self, preset: int | None = None, make_default: bool = False
    ) -> ServiceResponse:
        """The ``save_preset`` action: store the current view; responds with its slot.

        Without ``preset`` the lowest free slot; with it, that slot is overwritten.
        """
        self._require_presets()
        index = await ptz.async_save_preset(
            self.coordinator,
            self._serial,
            self._presets,
            preset=preset,
            make_default=make_default,
            name=SERVICE_SAVE_PRESET,
        )
        return {ATTR_PRESET: index}

    async def async_delete_preset(self, preset: int) -> None:
        """The ``delete_preset`` action: clear slot ``preset``.

        A slot the last read showed unset raises ``preset_not_set`` before sending.
        """
        self._require_presets()
        station = self.coordinator.station
        if station.presets(self._serial) is not None and preset not in presets.enabled_indexes(
            station, self._serial
        ):
            raise errors.preset_not_set(preset)
        await ptz.async_delete_preset(
            self.coordinator, self._serial, self._presets, preset, SERVICE_DELETE_PRESET
        )

    def _require_presets(self) -> None:
        """Refuse a preset action on a model without pan/tilt control or presets."""
        if not detections.has_pan_tilt_control(self._serial):
            raise errors.pan_tilt_unsupported()
        if not detections.has_preset_entities(self._serial):
            raise errors.presets_unsupported()

    async def async_zoom(self, direction: str) -> None:
        """The ``zoom`` action: the live-view zoom one step in or out, within 1x-12x."""
        streams = runtime.streaming(self.coordinator.config_entry)
        if not detections.has_pan_tilt_control(self._serial):
            raise errors.pan_tilt_unsupported()
        if (
            not detections.has_zoom(self._serial)
            or streams is None
            or not streams.has_camera(self._serial)
        ):
            raise errors.zoom_unsupported()
        step = ZOOM_ACTION_STEP if direction == ZOOM_IN else -ZOOM_ACTION_STEP
        zoom = min(MAX_ZOOM, max(MIN_ZOOM, streams.live_zoom(self._serial) + step))
        await ptz.async_set_live_zoom(self.coordinator, self._serial, zoom, SERVICE_ZOOM)

    async def async_record(self, duration: int | None = None) -> ServiceResponse:
        """The ``record`` action: ``duration`` seconds of the live stream into the history.

        Refused before anything opens with the history off, on a camera without a live
        stream, or while this camera records. A capture whose stream ended early is
        kept with ``complete`` False.
        """
        entry = self.coordinator.config_entry
        events = entry.runtime_data.history
        if not events.enabled:
            raise errors.recording_needs_history()
        streams = runtime.streaming(entry)
        broadcast = streams.broadcast(self._serial) if streams is not None else None
        if broadcast is None:
            raise errors.recording_unsupported()
        if self._attr_is_recording:
            raise errors.recording_in_progress()
        seconds = duration if duration is not None else _record_length(entry.options)

        async def produce(write: ClipWriter) -> MediaClip:
            return await broadcast.async_capture(seconds, write)

        self._attr_is_recording = True
        self.async_write_ha_state()
        try:
            async with asyncio.timeout(seconds + _RECORD_MARGIN_SECONDS):
                saved = await events.async_save_clip(self._serial, produce, kind=_LIVE_KIND)
        except EufySecurityError as err:
            raise errors.recording_failed_to_capture(err) from err
        except (history.ClipStoreError, OSError, TimeoutError) as err:
            # The type only: an OSError's text carries the clip's path and camera name.
            _LOGGER.warning(
                "A clip of %s could not be saved to the event history (%s)",
                redact_serial(self._serial),
                type(err).__name__,
            )
            raise errors.recording_failed() from err
        finally:
            self._attr_is_recording = False
            self.async_write_ha_state()
        return {
            ATTR_MEDIA_CONTENT_ID: history.media_content_id(self.hass, saved.path),
            ATTR_DURATION: saved.clip.duration_s,
            ATTR_COMPLETE: saved.clip.complete,
        }

    @override
    async def async_added_to_hass(self) -> None:
        """Follow this camera's image changes until the entity is removed."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                snapshots.image_signal(self.coordinator.config_entry.entry_id, self._serial),
                self._async_on_image,
            )
        )

    @callback
    def _async_on_image(self) -> None:
        """A new image: write the state; ``image_updated`` makes every image a new state."""
        self.async_write_ha_state()

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """The shown image's tier and times; never a path, serial or record id.

        ``triggered_at`` is the detection's own time, or, for a refreshed image, the
        start of the recording it came from. ``image_updated`` is when the image was
        stored and differs for every stored image, the same picture fetched again
        included. A camera with no image has neither ``image_source`` nor
        ``image_updated``, and Home Assistant answers its image request with an error.
        """
        attributes: dict[str, Any] = {}
        source = self._manager.source_for(self._serial)
        if source is not None:
            attributes[ATTR_IMAGE_SOURCE] = source.value
        when = self._manager.event_time_for(self._serial)
        if when is not None:
            attributes[ATTR_TRIGGERED_AT] = dt_util.utc_from_timestamp(when / 1000).isoformat(
                timespec="milliseconds"
            )
        elif (recorded := self._manager.recorded_time_for(self._serial)) is not None:
            attributes[ATTR_TRIGGERED_AT] = dt_util.as_utc(recorded).isoformat(
                timespec="milliseconds"
            )
        if (stored := self._manager.stored_time_for(self._serial)) is not None:
            attributes[ATTR_IMAGE_UPDATED] = stored.isoformat(timespec="milliseconds")
        return attributes

    @override
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """The cached image; with none, maybe a live keyframe in the background.

        A ``width``/``height`` request the small copy fits is answered with it, else
        the full image, which Home Assistant scales.
        """
        image = self._manager.image_for(self._serial)
        if image is None and self.available:
            self._manager.async_request_live(self.coordinator.station, self._serial)
        if image is not None and small_images.fits_small(width, height):
            return await self._manager.small_image_for(self._serial) or image
        return image

    @property
    @override
    def entity_picture(self) -> str:
        """The small copy's URL once there is an image, with the access token; before
        one, Home Assistant's ``camera_proxy`` URL."""
        version = self.small_image_version
        if version is None:
            return super().entity_picture
        return small_images.small_image_url(self.entity_id, version, self.access_tokens[-1])

    @property
    @override
    def small_image_version(self) -> str | None:
        """The shown image's store time; None before one."""
        stored = self._manager.stored_time_for(self._serial)
        return None if stored is None else str(int(stored.timestamp() * 1000))

    @override
    async def async_full_image(self) -> bytes | None:
        """The cached image; never fetches."""
        return self._manager.image_for(self._serial)

    @override
    async def async_small_image(self) -> bytes | None:
        """The small copy of the cached image."""
        return await self._manager.small_image_for(self._serial)
