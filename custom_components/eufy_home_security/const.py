"""Constants for the Anker eufy Home Security integration.

Everything about eufy itself lives in the ``eufy-home-security`` library; this
module holds only the Home Assistant side's own names.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "eufy_home_security"

# The station poll is a safety net, not the main path: guard-mode changes
# arrive by push. 45 s is the middle of a 30-60 s band.
POLL_INTERVAL_SECONDS: Final = 45
# The storage record's own read, on its own timer: disk use moves slowly, and the
# station pushes a new record whenever a format ends or another client reads it
# (the library guide's "about 30 min").
STORAGE_POLL_INTERVAL_SECONDS: Final = 30 * 60

# The alarm panel's translation key, and the key of its entity unique id.
GUARD_MODE_KEY: Final = "guard_mode"

# How long a detection sensor stays on after the detection's own time, in seconds.
# The station announces a detection but never its end.
CONF_DETECTION_HOLD: Final = "detection_hold"
DEFAULT_DETECTION_HOLD_SECONDS: Final = 10
MIN_DETECTION_HOLD_SECONDS: Final = 5
MAX_DETECTION_HOLD_SECONDS: Final = 300
# The alarm panel's safety net, in minutes: how long Triggered or Pending may stand
# with no alarm stop from the station.
CONF_ALARM_TIMEOUT: Final = "alarm_timeout"
DEFAULT_ALARM_TIMEOUT_MINUTES: Final = 10
MIN_ALARM_TIMEOUT_MINUTES: Final = 1
MAX_ALARM_TIMEOUT_MINUTES: Final = 60
# Which image a detection shows.
CONF_CAMERA_IMAGE: Final = "camera_image"


class CameraImageMode(StrEnum):
    """The Camera image option's three choices; the stored option value.

    The choices and the default are the library's "Suggested configuration" table
    (python-eufy_home_security docs/how-to/home-assistant.md, "Camera images").
    """

    # HD from the recording: the thumbnail, then the trigger frame replaces it.
    HD = "hd"
    # Fast thumbnail only: no playback, no ffmpeg.
    THUMBNAIL = "thumbnail"
    # HD only: the trigger frame, with no low-resolution image in between.
    HD_ONLY = "hd_only"


DEFAULT_CAMERA_IMAGE: Final = CameraImageMode.HD
# Off by default: a camera with no detection image may ask for a live keyframe, which
# wakes a battery camera, at most once per cooldown.
CONF_LIVE_SNAPSHOT: Final = "live_snapshot"
LIVE_SNAPSHOT_COOLDOWN_SECONDS: Final = 300
# On by default: one authenticated read with the saved session that says whether eufy
# still accepts it, never a sign-in. Without it a kick-out by another client is noticed
# only when a command next needs eufy, which on an account of HomeBases alone can be never.
CONF_SESSION_PROBE: Final = "session_probe"
# Days of stills kept as files in the media folder (history.py); 0 writes none.
CONF_EVENT_HISTORY_DAYS: Final = "event_history_days"
DEFAULT_EVENT_HISTORY_DAYS: Final = 7
MAX_EVENT_HISTORY_DAYS: Final = 365
# Each HomeBase recording also copied into the history as an MP4 (recordings.py); off
# by default. Needs the event history on.
CONF_EVENT_VIDEOS: Final = "event_videos"
# The record action's default length in seconds (HA's own camera.record uses 30 s).
# Read at each call, so a change applies without a reload.
CONF_RECORD_LENGTH: Final = "record_length"
DEFAULT_RECORD_LENGTH_SECONDS: Final = 30
MIN_RECORD_LENGTH_SECONDS: Final = 5
MAX_RECORD_LENGTH_SECONDS: Final = 300
# P2P sessions held to each HomeBase (the library's ``Station.max_sessions``); its range
# and default are the library's. Applied to running stations without a reload.
CONF_STATION_SESSIONS: Final = "station_sessions"
# eufy's cloud push (FCM), off by default: the only path a detection of a camera
# without a HomeBase takes to Home Assistant. Started after the platforms, in the
# background; a change reloads the entry.
CONF_CLOUD_PUSH: Final = "cloud_push"
# Every device-list fetch asks every eufy cloud region (the library's ``scan_regions``),
# off by default: a region that listed no devices is otherwise not asked again. A
# change reloads the entry.
CONF_SCAN_REGIONS: Final = "scan_regions"
# The country the account logs in with (ISO 3166 alpha-2), as the eufy app does: eufy
# lists a device only to a login with the country it is held under. Empty means Home
# Assistant's country. A change reloads the entry and asks every login scope once.
CONF_COUNTRY: Final = "country"
# Further countries the account signs in with, one sign-in each on the country's home
# region: a home shared from an account in another country is listed only under that
# country. A change reloads the entry and asks every login scope once.
CONF_EXTRA_COUNTRIES: Final = "extra_countries"
# The wait before a failed first push start is tried again, doubling up to the cap.
# The library restarts a listener that has listened once by itself.
PUSH_START_RETRY_MIN_SECONDS: Final = 60.0
PUSH_START_RETRY_MAX_SECONDS: Final = 30 * 60.0
# The repair issue for an event history written where the container does not keep it.
ISSUE_MEDIA_NOT_PERSISTENT: Final = "media_not_persistent"
# Home Assistant's page on its media folder, linked from that issue.
MEDIA_DOCS_URL: Final = "https://www.home-assistant.io/more-info/local-media/setup-media/"
# The first probe waits for the P2P starts to settle and keeps setup itself cloud-free.
SESSION_PROBE_FIRST_DELAY_SECONDS: Final = 60
# Four reads a day, next to the library's own 24 hourly ones on an account with an
# on-demand station. Not user-tunable: nobody is invited to a one-minute probe.
SESSION_PROBE_INTERVAL_SECONDS: Final = 6 * 60 * 60
# The options flow's only step. Deliberately not named ``STEP_*``: every ``STEP_``
# constant is looked up under ``config.step`` by tests/test_packaging.py, and this
# step lives under ``options.step``.
OPTIONS_STEP_INIT: Final = "init"
# The options form's collapsible sections, in form order; each holds the options
# OPTIONS_SECTIONS in config_flow.py names. The options are stored flat.
OPTIONS_SECTION_DETECTIONS: Final = "detections"
OPTIONS_SECTION_CAMERA_IMAGES: Final = "camera_images"
OPTIONS_SECTION_LIVE_VIEW: Final = "live_view"
OPTIONS_SECTION_HISTORY: Final = "history"
OPTIONS_SECTION_EUFY_ACCOUNT: Final = "eufy_account"
OPTIONS_SECTION_MORE_COUNTRIES: Final = "more_countries"

# Diagnostic entity keys, each the translation key and the key of its unique id.
BATTERY_KEY: Final = "battery"
SIGNAL_STRENGTH_KEY: Final = "signal_strength"
FIRMWARE_KEY: Final = "firmware"
EMMC_USED_KEY: Final = "emmc_used"
MODEL_KEY: Final = "model"
# The station's storage record (disk and eMMC), each the translation key and the key
# of its unique id. The disk ones exist once the record reports a disk, the eMMC ones
# once it reports the eMMC. The eMMC's used space is "emmc_used_space", not
# "emmc_used": that key is the station's eMMC use-% dump sensor, on the same serial.
DISK_USED_KEY: Final = "disk_used"
DISK_SIZE_KEY: Final = "disk_size"
DISK_FREE_KEY: Final = "disk_free"
DISK_USED_PERCENT_KEY: Final = "disk_used_percent"
DISK_TEMPERATURE_KEY: Final = "disk_temperature"
DISK_PROBLEM_KEY: Final = "disk_problem"
DISK_FORMATTING_KEY: Final = "disk_formatting"
EMMC_WEAR_KEY: Final = "emmc_wear"
EMMC_USED_SPACE_KEY: Final = "emmc_used_space"
EMMC_FREE_KEY: Final = "emmc_free"
EMMC_SIZE_KEY: Final = "emmc_size"
EMMC_PROBLEM_KEY: Final = "emmc_problem"
# Power and storage state from the parameter dump, each the key of its unique id;
# an entity exists once its device's state reports the field.
CHARGING_KEY: Final = "charging"
SOLAR_CHARGING_KEY: Final = "solar_charging"
SOLAR_INTENSITY_KEY: Final = "solar_intensity"
BATTERY_TEMPERATURE_KEY: Final = "battery_temperature"
WORKING_DAYS_KEY: Final = "working_days"
DETECTED_EVENTS_KEY: Final = "detected_events"
RECORDED_EVENTS_KEY: Final = "recorded_events"
BATTERY_LOW_KEY: Final = "battery_low"
STORAGE_PROBLEM_KEY: Final = "storage_problem"
# Raw library values shown as attributes, never mapped to text here.
ATTR_SOLAR_CHARGING: Final = "solar_charging"
ATTR_POWER_SOURCE: Final = "power_source"
ATTR_STORAGE_STATUS: Final = "storage_status"
# The station's subsystem versions by param id (the subsystems are unnamed).
ATTR_SUBSYSTEM_FIRMWARE: Final = "subsystem_firmware"
# The raw per-mode siren action, by lower-case guard mode name.
ATTR_SIREN_ACTIONS: Final = "siren_actions"

# The detection event entity's translation key and unique-id key: one per camera,
# fired once per detection the library delivers.
DETECTION_EVENT_KEY: Final = "detection"
# A camera's detection binary sensors, each the translation key and the key of its
# unique id: on at a detection of its class, off a hold later.
MOTION_DETECTED_KEY: Final = "motion_detected"
PERSON_DETECTED_KEY: Final = "person_detected"
PET_DETECTED_KEY: Final = "pet_detected"
VEHICLE_DETECTED_KEY: Final = "vehicle_detected"
# The bus event for every push no entity consumes.
EVENT_EUFY_HOME_SECURITY: Final = "eufy_home_security_event"
# The detection's own time as an ISO timestamp, never its arrival time.
ATTR_TRIGGERED_AT: Final = "triggered_at"
# The name eufy recognised, on an identified person only.
ATTR_PERSON_NAME: Final = "person_name"
# The station's alarm lifecycle event: triggered, stopped, delay.
ALARM_EVENT_KEY: Final = "alarm"
# The station's guard-mode change event, fired only from an authenticated push.
ARMING_EVENT_KEY: Final = "arming"
# A catalogued doorbell's ring event.
DOORBELL_EVENT_KEY: Final = "doorbell"
# Who changed the guard mode, as a source label (keypad, key fob, app), never the
# sender-supplied user name.
ATTR_CHANGED_BY: Final = "changed_by"
# Whether the push behind an alarm event was authenticated (GCM or cloud). An ECB
# trigger or delay still fires the alarm event, marked False, so an automation can
# refuse one that could be forged.
ATTR_AUTHENTICATED: Final = "authenticated"
# Who stopped the alarm, on an alarm_stopped fired from the library's AlarmChanged:
# the library's AlarmStopSource name (app, keypad, homebase), absent when unknown.
ATTR_STOP_SOURCE: Final = "stop_source"
# The guard mode the user selected, beside the panel state that shows the mode in
# force: "schedule" while a schedule drives the station.
ATTR_SELECTED_MODE: Final = "selected_mode"

STEP_USER: Final = "user"
# Reauth: the new password, then (only when another client holds the session) an
# explicit confirmation before Home Assistant takes it back.
STEP_REAUTH_CONFIRM: Final = "reauth_confirm"
STEP_REAUTH_TAKE_OVER: Final = "reauth_take_over"
# The user-started sign-in from the entry's menu; the take-over
# confirmation above is shared with reauth.
STEP_RECONFIGURE: Final = "reconfigure"
# The code of eufy's two-step verification, after any of the steps above signed in.
STEP_VERIFY_CODE: Final = "verify_code"
CONF_VERIFY_CODE: Final = "verify_code"

# Config-flow form errors (config.error in strings.json). Each string is a remedy.
ERROR_INVALID_EMAIL: Final = "invalid_email"
ERROR_LOGIN_CHALLENGE: Final = "login_challenge"
ERROR_INVALID_AUTH: Final = "invalid_auth"
ERROR_LOGIN_LIMITED: Final = "login_limited"
ERROR_SESSION_REPLACED: Final = "session_replaced"
ERROR_CANNOT_CONNECT: Final = "cannot_connect"
# eufy did not take the two-step code (wrong or expired); a new one may be on its way.
ERROR_INVALID_VERIFY_CODE: Final = "invalid_verify_code"
# eufy accepted the sign-in, then refused the session even after one more sign-in.
ERROR_SESSION_REJECTED: Final = "session_rejected"

# Translated exceptions (the top-level "exceptions" key of strings.json).
EXC_AUTH_FAILED: Final = "auth_failed"
# An arm or disarm the station answered but did not apply.
EXC_GUARD_MODE_NOT_APPLIED: Final = "guard_mode_not_applied"
# A setting write the station answered but did not apply. The entity keeps
# the last value it read, so nothing shows a value the station never took.
EXC_SETTING_NOT_APPLIED: Final = "setting_not_applied"
# A setting write for a device the station cannot address: eufy's device list does
# not pair it to this station, or its cloud record names no slot. Both are ordinary
# runtime states rather than programming errors, so both are named.
EXC_SETTING_DEVICE_UNAVAILABLE: Final = "setting_device_unavailable"
# A per-mode action or delay write the library refused before sending anything: the
# mode's whole table cannot be written back unchanged (a device reports no action for
# the mode, is of no known kind, or devices hold different delays for it).
EXC_SETTING_MODE_TABLE_REFUSED: Final = "setting_mode_table_refused"
# A setting write that timed out: it may still apply, the next poll shows whether it did.
EXC_SETTING_UNCONFIRMED: Final = "setting_unconfirmed"
# A setting value the library refused before sending (off its step, outside its domain).
EXC_SETTING_VALUE_INVALID: Final = "setting_value_invalid"
# An arm or disarm that could not reach the station.
EXC_STATION_UNREACHABLE: Final = "station_unreachable"
# The same for a station that connects on demand (a battery camera without a
# HomeBase): it sleeps between commands, so the remedy is to try again.
EXC_ON_DEMAND_UNREACHABLE: Final = "on_demand_unreachable"
# A command or capture that failed while another client's login had ended the eufy
# session: waking a battery camera needs a key from eufy, which
# refuses the ended session. The remedy is the account's repair, not the camera.
EXC_SESSION_REPLACED_SEE_REPAIRS: Final = "session_replaced_see_repairs"
# An arm or disarm refused because the station rejects even its re-fetched key.
EXC_STATION_KEY_REJECTED: Final = "station_key_rejected"
# An arm or disarm refused because eufy serves the station a key that cannot be used.
EXC_STATION_KEY_UNUSABLE: Final = "station_key_unusable"
# An arm or disarm that needed eufy's cloud, which refused or was down.
EXC_CLOUD_UNAVAILABLE: Final = "cloud_unavailable"
# No cached device list and no cloud to fetch one from: setup retries.
EXC_CACHE_UNAVAILABLE: Final = "cache_unavailable"
# A "Refresh device list" press whose cloud fetch failed; nothing changed.
EXC_DEVICE_LIST_REFRESH_FAILED: Final = "device_list_refresh_failed"
# A preset or live capture pressed while the library holds the camera for another
# capture; nothing was sent. The busy rule is the library's.
EXC_CAPTURE_IN_PROGRESS: Final = "capture_in_progress"
# A preset capture for a slot the last read showed unset; nothing was sent.
EXC_PRESET_NOT_SET: Final = "preset_not_set"
# A preset action on a camera whose model has no pan/tilt presets.
EXC_PRESETS_UNSUPPORTED: Final = "presets_unsupported"
# A default-preset write the camera refused with -502, its "set anyway?" question.
# Never answered with confirm on the user's behalf.
EXC_DEFAULT_PRESET_NEEDS_CONFIRMATION: Final = "default_preset_needs_confirmation"
# A pan/tilt step the camera refused: it was still moving after the library's retries.
EXC_PAN_TILT_NOT_APPLIED: Final = "pan_tilt_not_applied"
EXC_PAN_TILT_UNSUPPORTED: Final = "pan_tilt_unsupported"
# The zoom action on a pan/tilt camera whose model has no zoom.
EXC_ZOOM_UNSUPPORTED: Final = "zoom_unsupported"
EXC_ZOOM_NEEDS_SINGLE_VIEW: Final = "zoom_needs_single_view"
# A camera command the station refused with receipt -108: it does not handle it.
EXC_PTZ_COMMAND_NOT_HANDLED: Final = "ptz_command_not_handled"
# A save into a camera that already stores its most slots; nothing was sent when the
# last read showed it.
EXC_PRESETS_FULL: Final = "presets_full"
# A save the camera took without the read-back showing the slot stored.
EXC_PRESET_NOT_SAVED: Final = "preset_not_saved"
# A save with make_default: stored, but the read-back does not show it as default.
EXC_PRESET_SAVED_NOT_DEFAULT: Final = "preset_saved_not_default"
# A delete the camera took while the read-back still shows the slot in use.
EXC_PRESET_NOT_DELETED: Final = "preset_not_deleted"
# A slot index the camera does not have; nothing was sent.
EXC_PRESET_SLOT_UNKNOWN: Final = "preset_slot_unknown"
# The record action: one capture per camera at a time; nothing was started.
EXC_RECORDING_IN_PROGRESS: Final = "recording_in_progress"
# The record action with the event history off: a clip has nowhere to go.
EXC_RECORDING_NEEDS_HISTORY: Final = "recording_needs_history"
# The record action on a camera without a live stream.
EXC_RECORDING_UNSUPPORTED: Final = "recording_unsupported"
# A live open past the sessions per HomeBase option (LiveStreamLimitError).
EXC_LIVE_STREAM_LIMIT: Final = "live_stream_limit"
# A capture the camera did not deliver: not woken, or no keyframe in time.
EXC_CAMERA_UNAVAILABLE: Final = "camera_unavailable"
# A clip the camera delivered but that could not be stored (ffmpeg or the media folder).
EXC_RECORDING_FAILED: Final = "recording_failed"

# Repair issue translation keys (the top-level "issues" key of strings.json). Each
# issue id is the key followed by the entry id, and for a station's issue the
# station's device-registry id: never a serial.
# Another client's login ended the session; fixable by one confirmed login.
ISSUE_SESSION_REPLACED: Final = "session_replaced"
# eufy is refusing sign-ins, with a known wait.
ISSUE_LOGIN_LIMITED: Final = "login_limited"
# The same, when eufy gave no wait: no English is ever passed as a placeholder.
ISSUE_LOGIN_LIMITED_NO_WAIT: Final = "login_limited_no_wait"
# The library's own login budget on one cluster is spent; eufy refused nothing.
ISSUE_LOGIN_BUDGET: Final = "login_budget"
# A station rejects even its re-fetched key; fixable by releasing the latch.
ISSUE_KEY_REJECTED: Final = "key_rejected"
# A station's key or owner id was fetched again without a sign-in.
ISSUE_CREDENTIALS_REFRESHED: Final = "credentials_refreshed"
# The same, and it cost one eufy sign-in.
ISSUE_CREDENTIALS_REFRESHED_LOGIN: Final = "credentials_refreshed_login"
# A station stamps its records with another owner account, so it may drop our
# commands silently. Neither account id is ever kept or shown.
ISSUE_ACCOUNT_ID_MISMATCH: Final = "account_id_mismatch"
# Cloud push is switched on but not listening; the library keeps retrying.
ISSUE_PUSH_NOT_RUNNING: Final = "push_not_running"
# The cloud holds no key for the cipher a station named, under the station's owner.
ISSUE_CIPHER_UNAVAILABLE: Final = "cipher_unavailable"
# eufy serves a station a key that does not parse (the legacy RSA handshake); no
# re-fetch helps, so the issue is not fixable.
ISSUE_KEY_UNUSABLE: Final = "key_unusable"
# The account lists no devices in any eufy cloud region, and none is asked again.
ISSUE_NO_DEVICES: Final = "no_devices"
# Invitations sent to the account that it has not accepted in the eufy app.
ISSUE_PENDING_INVITES: Final = "pending_invites"

# Camera snapshots. The camera entity's unique-id key; it has no name of its
# own, the camera is its device.
CAMERA_KEY: Final = "camera"
# The per-camera "Capture live image" button's translation key and the key of its unique
# id: one live keyframe on demand, which wakes a battery camera.
CAPTURE_LIVE_IMAGE_KEY: Final = "capture_live_image"
# The "Refresh image" button's translation key and unique-id key: the
# image of the camera's newest recorded event, per the Camera image option. It never
# wakes a camera paired to a HomeBase; it wakes a standalone one (its still is a command).
REFRESH_IMAGE_KEY: Final = "refresh_image"
# The account's "Refresh device list" button's translation key and unique-id key:
# one cloud device-list fetch per press.
REFRESH_DEVICE_LIST_KEY: Final = "refresh_device_list"
# Pan/tilt presets. The per-camera "Refresh presets" CONFIG
# button's translation key and unique-id key: one slot read per press, which wakes a
# battery camera; never pressed by setup.
REFRESH_PRESETS_KEY: Final = "refresh_presets"
# The per-slot "Capture preset n" button's translation key (its unique-id key carries
# the slot index, see presets.preset_capture_key), and the camera entity service's
# name.
CAPTURE_PRESET_KEY: Final = "capture_preset"
SERVICE_CAPTURE_PRESET: Final = CAPTURE_PRESET_KEY
# The per-slot "Preset n image" image entity's translation key.
PRESET_IMAGE_KEY: Final = "preset_image"
# The per-camera "Default preset" CONFIG select's translation key and unique-id key:
# the slot the camera returns to on its own when idle.
DEFAULT_PRESET_KEY: Final = "default_preset"
# The live-view preset select, and its option for "wherever the camera stands".
LIVE_PRESET_KEY: Final = "live_preset"
LIVE_PRESET_CAMERA_DEFAULT: Final = "camera_default"
# The pan/tilt step buttons' translation and unique-id keys, one fixed step each.
PAN_LEFT_KEY: Final = "pan_left"
PAN_RIGHT_KEY: Final = "pan_right"
TILT_UP_KEY: Final = "tilt_up"
TILT_DOWN_KEY: Final = "tilt_down"
# The "Save current view" button's translation and unique-id key.
SAVE_VIEW_KEY: Final = "save_view"
# How long a HomeBase session may be down before its entities show unavailable: the
# library reconnects within about a second, so a drop that recovers within this is no
# outage (library guide, Availability).
CONNECTION_LOSS_GRACE_SECONDS: Final = 10
# The live-view zoom number's translation and unique-id key.
LIVE_ZOOM_KEY: Final = "live_zoom"
# The live-view zoom slider's step, and the step of one zoom action.
LIVE_ZOOM_STEP: Final = 0.5
ZOOM_ACTION_STEP: Final = 1.0
# The camera entity's pan/tilt/zoom actions, for camera cards.
SERVICE_PAN_TILT: Final = "pan_tilt"
SERVICE_GOTO_PRESET: Final = "goto_preset"
SERVICE_ZOOM: Final = "zoom"
SERVICE_SAVE_PRESET: Final = "save_preset"
SERVICE_DELETE_PRESET: Final = "delete_preset"
# The camera entity's record action, its field and its response keys.
SERVICE_RECORD: Final = "record"
ATTR_DURATION: Final = "duration"
ATTR_MEDIA_CONTENT_ID: Final = "media_content_id"
ATTR_COMPLETE: Final = "complete"
# A HA-side cap on one remux of a clip to MP4 (a stream copy, no decode).
CLIP_REMUX_TIMEOUT_SECONDS: Final = 60
# Recordings sync: the catch-up interval, the first pass after setup (keeps setup
# itself free of station queries), the clip length assumed when the camera reports
# none, the attempts per recording, and a HA-side cap on one download.
RECORDING_SYNC_INTERVAL_SECONDS: Final = 15 * 60
RECORDING_SYNC_FIRST_DELAY_SECONDS: Final = 60
RECORDING_DEFAULT_CLIP_SECONDS: Final = 30
RECORDING_MAX_ATTEMPTS: Final = 3
RECORDING_DOWNLOAD_TIMEOUT_SECONDS: Final = 300
# The save_preset action's second field.
ATTR_MAKE_DEFAULT: Final = "make_default"
# The highest slot index a preset action accepts: a pan/tilt camera reports ten slots.
PRESET_MAX_INDEX: Final = 9
# The pan_tilt and zoom actions' field, and its values.
ATTR_DIRECTION: Final = "direction"
ZOOM_IN: Final = "in"
ZOOM_OUT: Final = "out"
# The capture_preset and goto_preset actions' field: the slot index as the camera reports it.
ATTR_PRESET: Final = "preset"
# The preset image entity's one attribute: its slot index, never a path.
ATTR_PRESET_INDEX: Final = "preset_index"
# A HA-side cap on one whole preset job (Station.async_preset_image): a cold wake of
# up to ~10 s, the go-to, the library's own stream idle bound and its settle, with
# margin. The library bounds each step itself; this only caps the whole.
PRESET_CAPTURE_TIMEOUT_SECONDS: Final = 45
# A HA-side cap on one slot read (Station.async_refresh_presets), at the live
# keyframe's value: a wake plus one bounded query.
PRESET_REFRESH_TIMEOUT_SECONDS: Final = 30
# The account service device's name. A plain name, not a translation key: device-name
# translations resolve from a cache that may be cold at setup (device_registry.py).
ACCOUNT_DEVICE_NAME: Final = "eufy account"
# Which tier the shown image came from: thumbnail, trigger_frame, detection_live or live.
ATTR_IMAGE_SOURCE: Final = "image_source"
# The still-cache name of a camera entity's still; a preset's is ``preset_<index>``.
CAMERA_STILL_NAME: Final = "camera"
# When the shown image was stored, as an ISO timestamp: differs for every stored image.
ATTR_IMAGE_UPDATED: Final = "image_updated"
# Safety bounds on one media operation, so a station worker never hangs. The
# library bounds each step itself; these only cap the whole.
TRIGGER_FRAME_TIMEOUT_SECONDS: Final = 45
LIVE_SNAPSHOT_TIMEOUT_SECONDS: Final = 30
# The thumbnail tier (Station.async_event_thumbnail): the library's own bounds are a
# 15 s history query (StationSession.async_history_record) then a 12 s still fetch
# (the method's timeout default), 27 s in all. The cap sits just above that, at the
# live keyframe's value, so it never cuts a library-bounded step short and only
# catches a path the library leaves unbounded (e.g. a wait for the op lock).
THUMBNAIL_TIMEOUT_SECONDS: Final = 30
FFMPEG_DECODE_TIMEOUT_SECONDS: Final = 20
# The Refresh image job (Station.async_camera_image): the library walks the history back
# one page per day over CAMERA_IMAGE_DAYS = 7 days, each page bounded at 15 s
# (StationSession.async_list_history), 105 s in all, then fetches the still in at most
# 12 s: 117 s. The thumbnail cap sits just above that. The trigger-frame cap is the same
# 105 s walk plus TRIGGER_FRAME_TIMEOUT_SECONDS instead of the still fetch. Like the
# caps above, these only bound the worker: a page at its own bound raises inside the
# library first.
REFRESH_THUMBNAIL_TIMEOUT_SECONDS: Final = 120
REFRESH_TRIGGER_FRAME_TIMEOUT_SECONDS: Final = 150
# One deferred thumbnail attempt for a detection whose history row had no
# thumbnail yet and whose own trigger frame did not land. 60 s is the app's default
# clip length and the live cameras' value (clip_length, command 1249, range 10-120 s).
# Not measured.
THUMBNAIL_RETRY_DELAY_SECONDS: Final = 60
# A standalone camera's detection still (Station.async_event_image, THUMBNAIL): the
# library returns the camera's newest still only when it is the detection's; one not
# written yet is asked once more after the retry delay. The first attempt waits for the
# camera to write the still: two samples found an older still 4-5 s after the push and
# the detection's own 24 s after it.
STANDALONE_THUMBNAIL_DELAY_SECONDS: Final = 15
STANDALONE_IMAGE_RETRY_DELAY_SECONDS: Final = 20
# The largest decoded JPEG kept in memory; a 3840x2160 frame is well under 2 MB.
MAX_JPEG_BYTES: Final = 16 * 1024 * 1024
# The camera still proxy asks for a new image this often at most; the image is cached,
# so this bounds the work of a stream view, not the station's.
CAMERA_FRAME_INTERVAL_SECONDS: Final = 30

# Live video: the camera's MPEG-TS, served over Home Assistant's own
# HTTP server and passed through untouched, because nothing on this host may decode or
# re-encode video (measured on hardware: decoding one 4K HEVC stream is 0.58x
# realtime with four cores saturated, remuxing it about 2% of one core).
#
# The route the stream view answers on. The camera serial in the path is a deliberate
# exception to the redaction rule (the library's guide advises a random token): the
# endpoint serves loopback peers only, go2rtc's log is local, and the URL is never logged.
STREAM_URL_PATH: Final = "/api/eufy_home_security/stream/{device_sn}"
STREAM_VIEW_NAME: Final = "api:eufy_home_security:stream"
# The stream URL's query key for the instance secret; the stream component redacts it.
STREAM_AUTH_PARAM: Final = "auth"
# Headers a reverse proxy adds; a stream request carrying any of them is refused.
STREAM_FORWARDING_HEADERS: Final = (
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Real-IP",
)
# MPEG-TS. The library muxes it; the integration writes the bytes through.
STREAM_CONTENT_TYPE: Final = "video/mp2t"
# How long a capture waits for a live view it ends to close before proceeding anyway.
# Not a library timeout: the library bounds its own steps, and this only stops a slow
# close holding up a time-critical detection still.
STREAM_YIELD_TIMEOUT_SECONDS: Final = 5

# The setting and choice a setting takes effect under (``power_mode=custom``), present
# only on a setting the library marks with ``applies_when``.
ATTR_APPLIES_WHEN: Final = "applies_when"
# The library's label of the value in ``applies_when``, when it has one.
ATTR_APPLIES_WHEN_LABEL: Final = "applies_when_label"
# On a controlling setting: dependent setting key -> the option label it applies at.
ATTR_CONTROLS: Final = "controls"

# Setting keys whose write changes the picture size a camera sends: a running live view
# is ended so the next open starts at the new size (an MPEG-TS stream cannot carry a
# size change).
PICTURE_CHANGING_SETTINGS: Final = frozenset({"live_streaming_resolution"})
