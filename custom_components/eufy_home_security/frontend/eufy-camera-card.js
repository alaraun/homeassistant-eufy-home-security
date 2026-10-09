// eufy camera card: a tile-style header, a 16:9 picture that shows the still until live view is started,
// pan/tilt/zoom and presets on the picture (cameras that have them), its history, and the camera's settings (staged; Set sends).
// Entities come from the camera's device (translation_key, else the library key in the unique id), so only
// `entity` is required; the settings rows are built from whatever settings the device has: the common ones
// grouped, a setting that applies only in one state of another nested under it, the rest under More settings.

const CARD_VERSION = '2026.10.09-3';

const INVALID = ['unavailable', 'unknown', 'none', ''];
const DOMAIN = 'eufy_home_security';
// Settings writes return after the station's read-back; a battery camera behind a HomeBase may need a wake first.
const PENDING_MS = 30000;
// A still or preset capture at the HomeBase's session budget ends the slot holder's view first (a few seconds)
const ACT_PENDING_MS = 30000;
const FAILED_MS = 10000;
const LIVE_S = 120;
// No picture by then ends the view. HA's players report no video while they still retry or fall back,
// so their reports never end it. First picture: ~3 s behind a HomeBase, ~5 s from a sleeping battery camera.
const START_TIMEOUT_MS = 60000;
const HLS_RETRY_MS = 1000;
// A failed first playlist is asked again after HLS_RETRY_MS, doubling per retry; HLS without a picture by then
// is asked again: a cold first playlist answers in ~11 s, and a failed request
// (a network error on it) goes unreported by HA's HLS player
const HLS_STALL_MS = 20000;
const TOAST_MS = 4000;
// Moves (pan/tilt steps, go-to) waiting behind the running one; the integration returns a step after ~1.5 s
const MOVE_QUEUE = 3;
const COMPACT_W = 400;
// While the live picture plays, the controls over it fade this long after the last touch, click or mouse move on
// the picture (every width, full screen too); a tap or click on the picture brings them back
const CTL_HIDE_MS = 4000;
// Sections under the picture, as tabs: id, label, icon. One open at a time; pressing the open one closes it.
const SECTIONS = [['history', 'History', 'mdi:history'], ['settings', 'Settings', 'mdi:cog-outline']];
const IMG_CACHE = 40;
// Still request sizes (16:9): HA scales a camera JPEG to the smallest libjpeg factor at least this large
const STILL_WIDTHS = [640, 960, 1280, 1920];
// The integration's still history in HA's media folder: <media>/eufy_home_security/<camera name>/<file>.jpg
const HISTORY_ROOT = 'media-source://media_source/local/eufy_home_security';
// Tiles or station rows a History sub-tab shows first, and how many each Show more adds
const HISTORY_PAGE = 10;
// HA's date picker (ha-date-input) is lazy-loaded by HA: a card-helpers input_datetime row imports it. Loaded once per
// page; until it is defined (or after DATE_INPUT_MS without it) the browser's own date picker stands in.
const DATE_INPUT_MS = 4000;
let DATE_INPUT = null;
const dateInputReady = () => {
  if (!DATE_INPUT) {
    DATE_INPUT = (async () => {
      if (customElements.get('ha-date-input')) return true;
      try {
        if (window.loadCardHelpers) (await window.loadCardHelpers()).createRowElement({ entity: 'input_datetime.eufy_camera_card' });
      } catch (e) { /* the native picker stands in */ }
      return Promise.race([customElements.whenDefined('ha-date-input').then(() => true), new Promise(r => setTimeout(() => r(false), DATE_INPUT_MS))]);
    })().then((ok) => { if (!ok) DATE_INPUT = null; return ok; });
  }
  return DATE_INPUT;
};
// Resolved media URLs carry a signature HA accepts for a limited time: resolve again after this
const RESOLVE_MS = 30 * 60 * 1000;
// <date>_<time>_<camera>_<kind>.<ext>, date and time in HA's time zone; pictures and videos
const HISTORY_FILE = /^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})_(.+)\.(jpe?g|png|webp|mp4|m4v|webm|mov)$/i;
const VIDEO_EXT = /^(mp4|m4v|webm|mov)$/i;
// History sub-tabs: id, label, icon, text when empty. A file's kind decides its sub-tab (histTab); Station lists the
// recordings on the HomeBase's own storage instead (STATION_WS).
const HIST_TABS = [
  ['events', 'Events', 'mdi:motion-sensor', 'No saved events'],
  ['captures', 'Captures', 'mdi:camera-iris', 'No saved captures'],
  ['presets', 'Presets', 'mdi:crosshairs-gps', 'No saved preset pictures'],
  ['station', 'Station', 'mdi:harddisk', 'No recordings on the station'],
];
// The integration's websocket commands for the station's recordings: the list, and one recording fetched from the
// station into the history folder ({media_content_id, url}; the url plays at once)
const STATION_WS = 'eufy_home_security/recordings';
const STATION_FETCH_WS = 'eufy_home_security/recordings/fetch';
// The days back from today the integration lists station recordings for (its MAX_LIST_DAYS)
const STATION_DAYS = 30;
// camera entity -> false once the integration answered that the camera has no station recordings (a standalone
// camera); shared by the cards on the page, so the sub-tab stays hidden there
const STATION_SUPPORT = new Map();
// Station thumbnails that failed to load (404 when the still is absent): shown as an icon
const THUMB_BAD = new Set();
const histTab = kind => (kind === 'live' ? 'captures' : (/^preset_\d+$/.test(kind) ? 'presets' : 'events'));
const KINDS = {
  motion: ['Motion', 'mdi:motion-sensor'], person: ['Person', 'mdi:account'], identified_person: ['Known person', 'mdi:account-check'],
  stranger: ['Stranger', 'mdi:account-question'], pet: ['Pet', 'mdi:paw'], vehicle: ['Vehicle', 'mdi:car'],
  event: ['Event', 'mdi:image-outline'], live: ['Snapshot', 'mdi:camera-iris'],
};

// translation_key -> role, for the eufy_home_security integration (controls the card places itself)
const ROLES = {
  live_zoom: 'zoom', default_preset: 'defaultPreset',
  pan_left: 'panLeft', pan_right: 'panRight', tilt_up: 'tiltUp', tilt_down: 'tiltDown',
  capture_live_image: 'capture', refresh_image: 'refresh', detection: 'detection',
  motion_detected: 'motion', person_detected: 'person', pet_detected: 'pet', vehicle_detected: 'vehicle',
};
// Detection sensors in badge priority
const DETECT = [['person', 'Person', 'mdi:account'], ['vehicle', 'Vehicle', 'mdi:car'], ['pet', 'Pet', 'mdi:paw'], ['motion', 'Motion', 'mdi:motion-sensor']];
const EVENT_WORD = { motion: 'Motion', person: 'Person', identified_person: 'Known person', stranger: 'Stranger', pet: 'Pet', vehicle: 'Vehicle' };

// ---- Settings rows: every select/number/switch of the camera's device, keyed by the library's setting key ----
const ROW_DOMAINS = ['select', 'number', 'switch'];
// Rows without an entity category that still belong in Settings
const ROW_UNCATEGORISED = ['live_preset'];
// Guard-mode behaviour (per-mode actions, entry/leaving delays) belongs to the HomeBase and alarm, not the camera:
// a row only when `settings_include` names it
const ROW_SKIP = /^(camera|sensor)_action_|^(alarm|leaving)_delay_/;
// The main list, by group: id, heading, icon, keys in row order. A key no group names goes under More settings,
// unless `settings_include` names its entity (then Other).
const GROUPS = [
  ['picture', 'Picture', 'mdi:image-outline', ['nightvision_type_new', 'nightvision_type', 'spotlight_switch', 'spotlight_status',
    'spotlight_brightness_level', 'led_on_off', 'watermark_set']],
  ['detection', 'Detection', 'mdi:motion-sensor', ['motion_detection_status', 'detection_sensitivity',
    'detection_type_set', 'detection_type_set_1', 'detection_type_set_2', 'detection_type_set_3', 'detection_type_set_4',
    'disable_ptz_turn_switch']],
  ['recording', 'Recording', 'mdi:record-rec', ['live_streaming_resolution', 'record_resolution', 'audio_recording_on_off']],
  ['power', 'Power', 'mdi:battery-heart-variant', ['power_manager_mode', 'power_charge_mode']],
  ['ptz', 'Pan & tilt', 'mdi:pan', ['live_preset', 'default_preset', 'ai_tracking_status', 'ptz_turn_speed']],
  ['other', 'Other', 'mdi:tune-variant', []],
];
// Settings tabs in the card's order, More last. `settings_groups` lists the tabs to show in its order: a group it
// does not list is hidden (also one a later card version adds), an id the card does not know is ignored.
const MORE_GROUP = ['more', 'More', 'mdi:dots-horizontal'];
const GROUP_IDS = [...GROUPS.map(g => g[0]), MORE_GROUP[0]];
const groupInfo = id => (id === MORE_GROUP[0] ? MORE_GROUP : GROUPS.find(x => x[0] === id));
const shownGroups = (cfg) => {
  const v = cfg && cfg.settings_groups;
  if (v === undefined || v === null) return GROUP_IDS.slice();
  return [...new Set([].concat(v).map(String))].filter(id => GROUP_IDS.includes(id));
};
// Dependents in this order under their controller
const DEP_ORDER = ['video_clip_length', 'trigger_interval_time', 'motion_stop_end_early'];

const ROW_ICONS = {
  nightvision_type: 'mdi:weather-night', spotlight_switch: 'mdi:spotlight-beam', led_on_off: 'mdi:led-outline',
  motion_detection_status: 'mdi:motion-sensor', detection_sensitivity: 'mdi:motion-sensor', ai_tracking_status: 'mdi:target-account',
  disable_ptz_turn_switch: 'mdi:eye-off-outline', live_streaming_resolution: 'mdi:high-definition',
  audio_recording_on_off: 'mdi:microphone-outline', live_preset: 'mdi:crosshairs', default_preset: 'mdi:home-outline',
  speaker_volume: 'mdi:volume-high',
  record_resolution: 'mdi:quality-high', video_clip_length: 'mdi:filmstrip', trigger_interval_time: 'mdi:timer-refresh-outline',
  motion_stop_end_early: 'mdi:stop-circle-outline', power_manager_mode: 'mdi:battery-heart-variant', power_charge_mode: 'mdi:power-plug-battery-outline',
  watermark_set: 'mdi:watermark', detection_type_set: 'mdi:shape-outline', spotlight_brightness_level: 'mdi:brightness-6',
  // flags members of detection_type_set: 1 human, 2 vehicle, 3 pet, 4 other motion
  detection_type_set_1: 'mdi:account', detection_type_set_2: 'mdi:car', detection_type_set_3: 'mdi:paw', detection_type_set_4: 'mdi:shape-outline',
  microphone_on_off: 'mdi:microphone', speaker_on_off: 'mdi:speaker', anti_theft_detection_switch: 'mdi:shield-alert-outline',
  device_multiple_bridge_switch: 'mdi:access-point-network', device_multiple_bridge_mode: 'mdi:access-point-network',
  hb_connect_nas_switch: 'mdi:nas', hb_connect_nas_storage_type: 'mdi:nas', nas_stream_switch: 'mdi:nas',
  notification_type: 'mdi:bell-outline', switching_notification: 'mdi:bell-cog-outline', device_snooze_time: 'mdi:bell-sleep-outline',
  detection_sensitivity_test_mode: 'mdi:test-tube', device_silent_ota_switch: 'mdi:update', ptz_turn_speed: 'mdi:speedometer',
  time_format_set: 'mdi:clock-outline', view_mode: 'mdi:view-split-vertical', spotlight_status: 'mdi:lightbulb-on-outline',
  nightvision_type_new: 'mdi:weather-night',
};
// Labels where the entity's name says too little on the card ("Brightness" of what) or reads like an identifier
const ROW_LABELS = {
  live_preset: 'Live view opens at', power_charge_mode: 'Power source', spotlight_brightness_level: 'Spotlight brightness',
  detection_sensitivity_test_mode: 'Sensitivity test mode', device_multiple_bridge_switch: 'Multiple bridges',
};
// Sentence case for names given in title case ("Night Vision" -> "Night vision"); acronyms and CamelCase stay
const sentence = n => String(n).split(' ').map((w, i) => (i && /^[A-Z][a-z]+$/.test(w) ? w.toLowerCase() : w)).join(' ');
// Preset selects: option n is slot n (shown P<n+1>), `camera_default` the camera's own default
const PRESET_KEYS = ['live_preset', 'default_preset'];
// A write to these ends a running live view (the integration restarts it at the new picture size)
const RESTARTS_LIVE = /^live_streaming_resolution$/;
// Short option texts for segments (the full text stays in aria-label and the dropdown), and option icons
const OPT_SHORT = {
  'B&W Night Vision': 'B&W', 'Color Night Vision': 'Color', 'Full HD(1080P)': '1080p', '3K HD': '3K',
  'HD (720P)': '720p', 'Ultra 4K': '4K', '2K HD': '2K', Black: 'B&W', 'Optimal battery life': 'Battery', 'Optimal surveillance': 'Surveillance',
  'Custom recording': 'Custom', 'External Solar Panel': 'Solar panel', 'Timestamp and logo': 'Time + logo', Spotlights: 'Spotlight',
};
const OPT_ICONS = {
  'Optimal battery life': 'mdi:battery-heart-variant', 'Optimal surveillance': 'mdi:cctv', 'Custom recording': 'mdi:tune-variant',
};
// Segments for at most this many options, each short enough; a dropdown otherwise
const SEG_MAX = 4;
const SEG_CHARS = 12;
// entity id -> library setting key (unique id after `<serial>_`) for entities without a translation_key,
// shared by the cards on the page; KEY_ASKS holds the running registry request per id
const KEYS = new Map();
const KEY_ASKS = new Map();
// entity id -> its last reported `applies_when`: HA drops a state's attributes while it is unavailable
const APPLIES = new Map();
// entity id -> its last known state, shown while it is unavailable (a setting that does not apply now)
const LAST = new Map();
// entity id -> its last reported `applies_when_label` (the controller's option text)
const LABELS = new Map();
// Power readings in Settings -> Power: role, label, icon (translation_key or library key of the entity)
const POWER_STATS = [
  ['solarIntensity', 'Solar', 'mdi:white-balance-sunny'], ['batteryTemp', '', 'mdi:thermometer'],
  ['workingDays', 'since charge', 'mdi:calendar-clock'],
];
const STAT_KEYS = { solar_intensity: 'solarIntensity', battery_temperature: 'batteryTemp', working_days: 'workingDays',
  detected_events: 'detected', recorded_events: 'recorded', charging: 'charging', solar_charging: 'solarCharging' };

const ACTIONS = {
  capture: { label: 'New still', icon: 'mdi:camera-iris' },
  refresh: { label: 'Refresh event image', icon: 'mdi:image-refresh-outline' },
};

const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

// Clock time in the HA profile's format: time_format '12' | '24' | 'language' (its default) | 'system' (the browser's)
const clockTime = (hass, d) => {
  const loc = (hass && hass.locale) || {};
  const lang = loc.time_format === 'system' ? undefined : (loc.language || (hass && hass.language) || undefined);
  const o = { hour: '2-digit', minute: '2-digit' };
  if (loc.time_format === '12') o.hour12 = true;
  if (loc.time_format === '24') o.hourCycle = 'h23';
  return d.toLocaleTimeString(lang, o);
};
const human = (s) => { const t = String(s || '').replace(/_/g, ' '); return t.charAt(0).toUpperCase() + t.slice(1); };
const ok = (st) => !!st && !INVALID.includes(String(st.state).toLowerCase());
const num = (st) => (ok(st) ? parseFloat(st.state) : NaN);
const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const fmtZoom = (z) => (isNaN(z) ? '--' : `${Number.isInteger(z) ? z : z.toFixed(1)}×`);

class EufyCameraCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._stg = {};
    this._pending = {};
    this._keys = {};
    this._live = null; // { continuous, until, started, video, muted }
    this._busy = {};   // zoom calls in flight, by key
    this._moves = [];  // pan/tilt/go-to presses, the first one running: [{ key, service, data }]
    this._imgs = new Map(); // picture URL -> 'loading' | 'ok' | 'err'
    this._shown = {};       // picture slot -> last URL that loaded
    this._smalls = new Map(); // small picture path -> { url: object URL, '' after a failed fetch, null while loading }
    this._hist = null;      // { folder, items: [{ id, title, date, time, kind }], at, err } for the History section
    this._histUrls = new Map(); // media content id -> { url, at }
    this._view = null;      // history item shown in the picture
    this._htab = 'events';  // open History sub-tab
    this._hn = HISTORY_PAGE; // tiles shown in the open sub-tab (Show more adds HISTORY_PAGE)
    this._hfilt = {};       // sub-tab -> { pic, vid }: which media it shows (both by default)
    this._hday = null;      // History day (YYYY-MM-DD): only that day's files and recordings; null = the newest
    this._rec = null;       // { at } while this card's record action runs
    this._srec = null;      // station recordings: { items, supported, loading, err }
    this._sfetch = new Map(); // record id -> 'play' | 'save' while its fetch runs
    this._thumbs = new Set(); // station thumbnail URLs already shown (rendered with src at once)
    this._onVis = () => this._visibility();
    this._dd = null;        // setting key whose dropdown is open
  }

  static getConfigElement() { return document.createElement('eufy-camera-card-editor'); }
  static getStubConfig(hass) {
    const id = hass && Object.keys(hass.states).find(e => e.startsWith('camera.') && hass.entities && hass.entities[e]
      && hass.entities[e].platform === DOMAIN);
    return { type: 'custom:eufy-camera-card', entity: id || '' };
  }

  setConfig(config) {
    if (!config || !config.entity) throw new Error('entity (camera.*) is required');
    this._stop('');
    this._config = config;
    this._ents = null;
    this._stg = {};
    this._keys = {};
    this._autoDone = false;
    this._types = null;
    this._hist = null;
    this._view = null;
    this._srec = null;
    this._stationTab = undefined;
    this._hn = HISTORY_PAGE;
    this._hday = null;
    this.render();
  }

  set hass(hass) {
    const prev = this._hass;
    this._hass = hass;
    if (!prev || prev.entities !== hass.entities) this._ents = null;
    // An entity coming back from a restored state, or going to one, changes the rows
    else if (this._ents && prev.states !== hass.states) {
      const E0 = this._ents, rest = id => { const x = hass.states[id]; return !!x && !!x.attributes.restored; };
      if (E0.restored.some(id => !rest(id)) || E0.rows.some(r => rest(r.id))) this._ents = null;
    }
    if (this._stream && this._stream.tagName === 'HA-CAMERA-STREAM') {  // the single players take entityid only
      const st = hass.states[this._config.entity];
      if (st && this._stream.stateObj !== st) this._stream.stateObj = st;
      this._stream.hass = hass;
    }
    if (prev && this._open === 'history') {
      const a = prev.states[this._config.entity], b = hass.states[this._config.entity];
      if (a && b && a.attributes.image_updated !== b.attributes.image_updated) this._loadHistory();
    }
    if (prev && this.content && this._ents && !Object.keys(this._pending).length && !this._changed(prev, hass)) { this._autoTry(); return; }
    this.render();
    this._autoTry();
  }

  // Preloads a picture so the old one stays until the new one is ready. Returns { src, err } for the slot.
  _image(slot, url) {
    if (!url) { delete this._shown[slot]; return { src: null, err: true }; }
    let s = this._imgs.get(url);
    if (!s) {
      if (this._imgs.size > IMG_CACHE) [...this._imgs.keys()].slice(0, this._imgs.size - IMG_CACHE).forEach(k => this._imgs.delete(k));
      this._imgs.set(url, s = 'loading');
      const im = new Image();
      im.onload = () => { this._imgs.set(url, 'ok'); this.render(); };
      im.onerror = () => { this._imgs.set(url, 'err'); this.render(); };
      im.src = url;
    }
    if (s === 'ok') this._shown[slot] = url;
    const src = this._shown[slot] || null;
    return { src, err: !src && s === 'err' };
  }

  // The entity picture with a cache-buster: the URL itself only changes when the access token rotates.
  // A camera's `image_updated` changes with every stored still; other entities use `last_updated`.
  // Scaled: the camera's camera_proxy at the card's width bucket (its entity_picture is the small copy).
  _picUrl(st, scaled) {
    const u = st && st.attributes.entity_picture;
    if (!u) return null;
    const v = st.attributes.image_updated || st.last_updated || '';
    let q = `v=${encodeURIComponent(v)}`;
    if (scaled) {
      // The integration's entity_picture is its small copy; the full still comes from camera_proxy
      const tok = st.attributes.access_token;
      const base = u.includes('/small?') && tok ? `/api/camera_proxy/${st.entity_id}?token=${encodeURIComponent(tok)}` : u;
      // camera_proxy scales only with both width and height; one bucket per card size so a resize rarely refetches
      const need = (this._width || 480) * Math.min(window.devicePixelRatio || 1, 2);
      const w = STILL_WIDTHS.find(x => x >= need) || STILL_WIDTHS[STILL_WIDTHS.length - 1];
      q += `&width=${w}&height=${Math.round((w * 9) / 16)}`;
      return `${base}${base.includes('?') ? '&' : '?'}${q}`;
    }
    return `${u}${u.includes('?') ? '&' : '?'}${q}`;
  }

  // A tile picture: the integration's small copy (entity_picture /api/eufy_home_security/image/<id>/small?v=..&token=..)
  // fetched by its token-free URL with the login header, so the browser keeps it (private, immutable) across token
  // rotations and reloads, and shown as an object URL. Other pictures, or a failed fetch, use _picUrl.
  _tileUrl(st) {
    const u = st && st.attributes.entity_picture;
    if (!u || !u.includes('/small?')) return this._picUrl(st);
    const path = u.replace(/&token=[^&]*/, '');
    let c = this._smalls.get(path);
    if (!c) {
      const h = this._hass;
      if (!h || !h.fetchWithAuth) return u;
      this._smalls.set(path, c = { url: null, id: st.entity_id });
      h.fetchWithAuth(path)
        .then(r => (r.ok ? r.blob() : Promise.reject(new Error(String(r.status)))))
        .then((b) => { c.url = URL.createObjectURL(b); this._dropSmalls(st.entity_id, path); this.render(); })
        .catch(() => { c.url = ''; this.render(); });
    }
    if (c.url === '') return u;
    if (c.url) return c.url;
    // Still loading: the entity's previous small picture stays
    const prevUrl = [...this._smalls.values()].reverse().find(x => x.id === st.entity_id && x.url);
    return prevUrl ? prevUrl.url : null;
  }

  // Releases the entity's older small pictures once `keep` has loaded.
  _dropSmalls(id, keep) {
    for (const [k, v] of this._smalls) {
      if (v.id !== id || k === keep) continue;
      if (v.url) URL.revokeObjectURL(v.url);
      this._smalls.delete(k);
    }
  }

  getCardSize() { return 6; }
  getGridOptions() { return { columns: 12, min_columns: 6 }; }

  connectedCallback() {
    this._autoDone = false;
    if (this._ro && this.content) this._ro.observe(this.content);
    document.addEventListener('visibilitychange', this._onVis);
    if (this.content) this.render();
    this._autoTry();
  }

  disconnectedCallback() {
    // Leaving the view ends live view: nothing keeps a battery camera awake without a viewer
    this._stop('');
    clearTimeout(this._timer);
    this._timer = null;
    clearTimeout(this._toastT);
    clearTimeout(this._quietT);
    clearInterval(this._recT);
    this._recT = null;
    if (this._thumbIo) this._thumbIo.disconnect();
    this._moves = this._moves.slice(0, 1);
    this._watchOutside(false);
    this._menuOpen = false;
    this._dd = null;
    document.removeEventListener('visibilitychange', this._onVis);
    if (this._ro) this._ro.disconnect();
  }

  // ================== ENTITIES ==================
  // The camera's siblings from the entity registry (device + translation_key); `<role>_entity` config keys override.
  // rows: the settings rows' entities { id, key } (see _rowPick); byKey: every sibling's key -> entity id.
  _entities() {
    if (this._ents) return this._ents;
    const h = this._hass, c = this._config;
    const out = { camera: c.entity, presetImages: {}, presetButtons: {}, rows: [], byKey: {}, rowIds: {}, restored: [] };
    const reg = (h && h.entities) || {};
    const me = reg[c.entity];
    const dev = me && me.device_id;
    out.device = dev && h.devices ? h.devices[dev] : null;
    // The media station: the HomeBase a camera sits behind, or the camera itself when standalone
    out.station = out.device ? (out.device.via_device_id || out.device.id) : c.entity;
    const siblings = dev ? Object.values(reg).filter(e => e.device_id === dev && e.entity_id !== c.entity && !e.disabled_by) : [];
    const keys = this._keysOf(siblings.filter(e => !e.translation_key && ROW_DOMAINS.concat(['sensor', 'binary_sensor']).includes(e.entity_id.split('.')[0])
      && (e.entity_category === 'config' || e.entity_category === 'diagnostic' || e.entity_id.startsWith('binary_sensor.'))).map(e => e.entity_id));
    const inc = new Set([].concat(c.settings_include || [])), exc = new Set([].concat(c.settings_exclude || []));
    siblings.forEach((e) => {
      const id = e.entity_id, tk = e.translation_key, st = h.states[id];
      const dom = id.split('.')[0];
      // A state HA only restored: the integration does not provide the entity (or has not loaded yet). An orphan
      // may share its key with the entity that replaced it, so it is skipped everywhere.
      if (st && st.attributes.restored) { out.restored.push(id); return; }
      if (tk === 'preset_image') { const i = st && st.attributes.preset_index; if (i !== undefined) out.presetImages[i] = id; return; }
      if (tk === 'capture_preset') { const m = id.match(/_(\d+)$/); if (m) out.presetButtons[m[1]] = id; return; }
      const role = ROLES[tk];
      // Settings mirrored as read-only sensors reuse the control's translation_key: take the writable domain only
      if (role && (!out[role] || dom !== 'sensor')) out[role] = id;
      if (!tk && dom === 'sensor' && st && st.attributes.device_class === 'battery') out.battery = id;
      // Charging and power readings, by translation_key, else by device class
      const sk = STAT_KEYS[tk] || STAT_KEYS[keys[id]];
      if (sk && (dom === 'sensor' || dom === 'binary_sensor')) { out[sk] = id; return; }
      if (dom === 'binary_sensor' && !out.charging && st && st.attributes.device_class === 'battery_charging') { out.charging = id; return; }
      if (!ROW_DOMAINS.includes(dom)) return;
      const key = tk || keys[id];
      if (key === undefined) { out.keysPending = true; return; }
      if (!out.byKey[key] || dom !== 'sensor') out.byKey[key] = id;
      if (this._rowPick(e, key, inc, exc)) { out.rows.push({ id, key, inc: inc.has(id) }); out.rowIds[key] = id; }
    });
    Object.keys(ROLES).map(k => ROLES[k]).concat(['battery']).forEach((k) => { if (c[`${k}_entity`]) out[k] = c[`${k}_entity`]; });

    out.hasPan = !!(out.panLeft || out.panRight || out.tiltUp || out.tiltDown);
    this._ents = out;
    return out;
  }

  // A settings row by default: a configuration control of the camera (not a guard-mode action or delay),
  // not hidden in HA; `settings_include` / `settings_exclude` (entity ids) add and remove rows
  _rowPick(e, key, inc, exc) {
    if (exc.has(e.entity_id)) return false;
    if (inc.has(e.entity_id)) return true;
    if (e.hidden || e.hidden_by) return false;
    if (e.entity_category !== 'config' && !ROW_UNCATEGORISED.includes(key)) return false;
    return !ROW_SKIP.test(key);
  }

  // Library keys for entities the integration names from the library (no translation_key): the unique id is
  // `<serial>_<key>`. Returns the known ones; a registry request fills the rest and renders again.
  _keysOf(ids) {
    const out = {}, need = [], waits = new Set();
    ids.forEach((id) => {
      if (KEYS.has(id)) out[id] = KEYS.get(id);
      else if (KEY_ASKS.has(id)) waits.add(KEY_ASKS.get(id));
      else need.push(id);
    });
    const h = this._hass;
    if (need.length && h && h.callWS) {
      const fallback = id => id.split('.')[1];
      const ask = Promise.resolve(h.callWS({ type: 'config/entity_registry/get_entries', entity_ids: need })).then((r) => {
        need.forEach((id) => {
          const u = r && r[id] && r[id].unique_id;
          KEYS.set(id, typeof u === 'string' && u.includes('_') ? u.slice(u.indexOf('_') + 1) : fallback(id));
        });
      }).catch(() => need.forEach(id => KEYS.set(id, fallback(id)))).finally(() => need.forEach(id => KEY_ASKS.delete(id)));
      need.forEach(id => KEY_ASKS.set(id, ask));
      waits.add(ask);
    } else need.forEach((id) => { out[id] = id.split('.')[1]; });
    waits.forEach(p => p.then(() => { this._ents = null; if (this.content) this.render(); }));
    return out;
  }

  _watched() {
    const E = this._entities();
    const ids = [E.camera, E.battery, E.charging, E.solarCharging, E.detected, E.recorded, ...POWER_STATS.map(x => E[x[0]])];
    Object.keys(ROLES).forEach(k => ids.push(E[ROLES[k]]));
    E.rows.forEach(r => ids.push(r.id));
    Object.values(E.presetImages).forEach(id => ids.push(id));
    return ids.filter(Boolean);
  }

  _changed(prev, hass) {
    if (prev.states === hass.states) return false;
    return this._watched().some(id => prev.states[id] !== hass.states[id]);
  }

  _st(id) { return id && this._hass ? this._hass.states[id] : undefined; }

  // ================== HISTORY ==================
  // The camera's folder name as the integration writes it: the device name, runs of other than letters,
  // digits, `_` and `-` as one `_`, trimmed of `_`, at most 60 characters
  _historyFolder() {
    if (this._config.history_folder) return String(this._config.history_folder);
    const d = this._entities().device;
    const n = d && (d.name_by_user || d.name);
    const s = String(n || '').replace(/[^\p{L}\p{N}_-]+/gu, '_').replace(/^_+|_+$/g, '').slice(0, 60).replace(/^_+|_+$/g, '');
    return s || 'camera';
  }

  _parseHistory(folder, child) {
    const m = HISTORY_FILE.exec(child.title || '');
    if (!m) return null;
    const rest = m[5];
    const kind = rest.startsWith(`${folder}_`) ? rest.slice(folder.length + 1) : rest.split('_').pop();
    const video = VIDEO_EXT.test(m[6]) || child.media_class === 'video' || /^video\//.test(child.media_content_type || '');
    return { id: child.media_content_id, title: child.title, date: m[1], time: `${m[2]}:${m[3]}`, stamp: `${m[1]}_${m[2]}${m[3]}${m[4]}`,
      kind, video, thumb: child.thumbnail || null };
  }

  // Lists the camera's history folder, newest first: pictures and videos of every kind (sorted into sub-tabs on render)
  _loadHistory() {
    const h = this._hass;
    if (!h || !h.callWS) return;
    const folder = this._historyFolder();
    const seq = (this._histSeq || 0) + 1;
    this._histSeq = seq;
    if (!this._hist || this._hist.folder !== folder) this._hist = { folder, items: null };
    h.callWS({ type: 'media_source/browse_media', media_content_id: `${HISTORY_ROOT}/${folder}` }).then((r) => {
      if (this._histSeq !== seq) return;
      const items = (r.children || []).filter(c => c.can_play && /^(image|video)\//.test(c.media_content_type || 'image/'))
        .map(c => this._parseHistory(folder, c)).filter(Boolean)
        // newest first; at the same second the picture before its video
        .sort((a, b) => (a.stamp !== b.stamp ? (a.stamp < b.stamp ? 1 : -1) : a.video - b.video));
      // A video without a thumbnail shows the picture saved at the same second, when there is one
      const pics = new Map(items.filter(x => !x.video).map(x => [x.stamp, x.id]));
      items.forEach((x) => { if (x.video && !x.thumb) x.posterId = pics.get(x.stamp) || null; });
      this._hist = { folder, items, at: Date.now() };
      this.render();
    }).catch(() => {
      if (this._histSeq !== seq) return;
      // No folder yet (no still saved, history off) reads the same as an empty one
      this._hist = { folder, items: [], at: Date.now() };
      this.render();
    });
  }

  // A signed URL for a history file; null until resolved (resolving renders again)
  _histUrl(id) {
    const c = this._histUrls.get(id);
    if (c && c.url && Date.now() - c.at < RESOLVE_MS) return c.url;
    if (c && c.pending) return c.url || null;
    const h = this._hass;
    if (!h || !h.callWS) return null;
    this._histUrls.set(id, { ...(c || {}), pending: true });
    h.callWS({ type: 'media_source/resolve_media', media_content_id: id }).then((r) => {
      this._histUrls.set(id, { url: r && r.url, at: Date.now() });
      this.render();
    }).catch(() => { this._histUrls.set(id, { url: null, at: Date.now(), err: true }); this.render(); });
    return c ? c.url || null : null;
  }

  _histLabel(it) {
    const today = this._today();
    const [y, mo, d] = it.date.split('-');
    const pm = /^preset_(\d+)$/.exec(it.kind);
    const k = pm ? [`Preset P${parseInt(pm[1], 10) + 1}`, 'mdi:crosshairs-gps'] : (KINDS[it.kind] || [human(it.kind), 'mdi:image-outline']);
    const day = it.date === today ? '' : `${d}.${mo}${y === today.slice(0, 4) ? '' : `.${y}`} `;
    // it.time is HH:MM in HA's time zone: format those digits, not an instant, so the browser's zone cannot shift it
    const [hh, mm] = it.time.split(':').map(Number);
    return { when: `${day}${clockTime(this._hass, new Date(2000, 0, 1, hh, mm))}`, kind: k[0], icon: k[1] };
  }

  // History day: only that day's files and station recordings show, newest first; null goes back to the newest
  _setDay(day) {
    this._hday = day || null;
    this._hn = HISTORY_PAGE;
    if (this._htab === 'station') this._loadStation();
    this.render();
  }

  // Opens HA's date dialog (ha-date-input), else the browser's date picker over the day button
  _pickDay(btn) {
    const items = (this._hist && this._hist.items) || [];
    const max = this._today();
    // Station: the integration's listing window; the local sub-tabs: the oldest saved file's day
    const back = new Date(`${max}T12:00:00`);
    back.setDate(back.getDate() - (STATION_DAYS - 1));
    const sMin = `${back.getFullYear()}-${String(back.getMonth() + 1).padStart(2, '0')}-${String(back.getDate()).padStart(2, '0')}`;
    const min = this._htab === 'station' ? sMin : (items.length ? items[items.length - 1].date : max);
    dateInputReady().then((ok) => {
      if (ok) {
        if (!this._di) {
          const di = document.createElement('ha-date-input');
          di.hidden = true;
          di.canClear = true;
          di.addEventListener('value-changed', (ev) => { ev.stopPropagation(); this._setDay(ev.detail && ev.detail.value); this._focus('hday'); });
          di.addEventListener('change', ev => ev.stopPropagation());
          this.shadowRoot.append(di);
          this._di = di;
        }
        const di = this._di;
        Object.assign(di, { locale: this._hass.locale, min, max, value: this._hday || undefined });
        if (typeof di._openDialog === 'function') { di._openDialog(); return; }
        const inp = di.shadowRoot && di.shadowRoot.querySelector('ha-input');
        if (inp) { inp.click(); return; }
      }
      let n = this._dn;
      if (!n) {
        n = document.createElement('input');
        n.type = 'date';
        n.className = 'hdn';
        n.tabIndex = -1;
        n.setAttribute('aria-hidden', 'true');
        n.addEventListener('change', () => { this._setDay(n.value); this._focus('hday'); });
        this.shadowRoot.append(n);
        this._dn = n;
      }
      const r = btn.getBoundingClientRect();
      Object.assign(n.style, { left: `${r.left}px`, top: `${r.bottom}px` });
      Object.assign(n, { min, max, value: this._hday || '' });
      try { n.showPicker(); } catch (e) { n.focus(); }
    });
  }

  // ---- the station's own recordings (sub-tab Station) ----
  // Lists them through the integration a page at a time, newest first: the first page when the sub-tab opens and on
  // refresh (the previous first page stays while loading), the next one on Show more (from the cursor `next`). A page
  // asks the station a few days only: one that adds nothing while older days remain is followed by the next at once.
  _loadStation(more) {
    const h = this._hass;
    if (!h || !h.callWS) return;
    const prev = this._srec;
    if (more && (!prev || !prev.items || !prev.next || prev.loading || prev.moreLoading)) return;
    const id = this._config.entity, seq = (this._sseq || 0) + 1;
    this._sseq = seq;
    const day = this._hday;
    const same = prev && prev.items && prev.day === day;
    const base = more ? prev.items : [];
    this._srec = more ? { ...prev, moreLoading: true }
      : { items: same ? prev.items.slice(0, HISTORY_PAGE) : null, supported: prev ? prev.supported : true, loading: true,
        err: null, more: false, next: null, day };
    this.render();
    const ask = (before) => {
      const req = { type: STATION_WS, entity_id: id, limit: HISTORY_PAGE };
      if (before) req.before = before;
      if (day) req.day = day;
      h.callWS(req).then((r) => {
        if (this._sseq !== seq) return;
        const supported = !!(r && r.supported);
        if (supported) STATION_SUPPORT.delete(id); else STATION_SUPPORT.set(id, false);
        // Hidden at once unless it is the open sub-tab (then from the next opening: the tabs stay under the pointer)
        if (!supported && this._htab !== 'station') this._stationTab = false;
        const rows = supported ? (Array.isArray(r.recordings) ? r.recordings : []).map(x => this._parseRecording(x)).filter(Boolean) : [];
        const had = new Set(base.map(x => x.rid));
        const added = rows.filter(x => !had.has(x.rid));
        const next = supported && r.more && r.next ? String(r.next) : null;
        if (!added.length && next && next !== before) { ask(next); return; }
        const items = [...base, ...added];
        // Show more kept focus: it moves to the first added row (Show more itself goes when the list is complete)
        const a = this.shadowRoot.activeElement;
        const fromMore = more && a && a.dataset && a.dataset.focus === 'smore';
        this._srec = { items, supported, loading: false, err: null, more: !!next, next, day };
        this.render();
        if (fromMore) {
          // The first enabled button of the first added row, else Show more
          const first = items[base.length];
          const to = (k) => { this._focus(k); const f = this.shadowRoot.activeElement; return !!f && f.dataset.focus === k; };
          if (!first || !(to(`hist-${first.id}`) || to(`rsave-${first.rid}`))) this._focus('smore');
        }
      }).catch((e) => {
        if (this._sseq !== seq) return;
        const msg = (e && e.message) || 'The station did not answer';
        if (more) { this._srec = { ...prev, moreLoading: false }; this._toast(msg); this.render(); return; }
        this._srec = { items: same ? prev.items : null, supported: true, loading: false, err: msg, more: false, next: null, day };
        if (same) this._toast(msg);
        this.render();
      });
    };
    ask(more ? prev.next : null);
  }

  // One row of the station list, with its start as date and HH:MM in HA's time zone (like a history file name)
  _parseRecording(x) {
    const ms = x ? Date.parse(x.started_at) : NaN;
    if (isNaN(ms) || typeof x.record_id !== 'number') return null;
    let date, time;
    try {
      const t = new Intl.DateTimeFormat('sv-SE', { timeZone: (this._hass.config && this._hass.config.time_zone) || undefined,
        year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date(ms));
      [date, time] = t.split(' ');
    } catch (e) { const d = new Date(ms); date = d.toISOString().slice(0, 10); time = d.toTimeString().slice(0, 5); }
    // No kind: the integration names the file `event`, so it reads Event here too
    return { id: `rec:${x.record_id}`, rid: x.record_id, date, time, kind: x.kind || 'event', video: true, station: true,
      thumb: x.thumb_url || null, dur: typeof x.duration_s === 'number' ? x.duration_s : null, settled: !!x.settled,
      mcid: x.media_content_id || null, saved: !!x.media_content_id };
  }

  // Fetches one recording from the station into the history folder (5-10 s; the station serves one at a time).
  // Play shows it in the picture area afterwards; Save only stores it. A stored one plays from the media library.
  _fetchRecording(rid, play) {
    const h = this._hass;
    const it = this._srec && this._srec.items && this._srec.items.find(x => x.rid === rid);
    if (!h || !it || this._sfetch.has(rid) || !it.settled) return;
    if (it.mcid) { if (play) this._showRecording(it); return; }
    this._sfetch.set(rid, play ? 'play' : 'save');
    this.render();
    h.callWS({ type: STATION_FETCH_WS, entity_id: this._config.entity, record_id: rid }).then((r) => {
      if (!r || !r.media_content_id) throw new Error('The station sent no file');
      it.mcid = r.media_content_id;
      it.saved = true;
      if (r.url) this._histUrls.set(r.media_content_id, { url: r.url, at: Date.now() });
      if (this._open === 'history') this._loadHistory();
      if (play) this._showRecording(it);
    }).catch(e => this._toast((e && e.message) || 'Not fetched from the station'))
      .finally(() => { this._sfetch.delete(rid); this.render(); });
  }

  _showRecording(it) {
    if (this._open !== 'history') return;
    if (this._live) { this._toast('Stop live view to play a recording'); return; }
    this._view = it.id;
    this.render();
    this._focus('hist-close');
  }

  // Station thumbnails load as their row scrolls into the list's view
  _lazyThumbs() {
    const list = this.content.querySelector('.slist');
    const imgs = list ? [...list.querySelectorAll('img[data-src]')] : [];
    if (this._thumbIo && this._thumbRoot !== list) { this._thumbIo.disconnect(); this._thumbIo = null; }
    if (!imgs.length) return;
    const load = (im) => { im.src = im.dataset.src; this._thumbs.add(im.dataset.src); im.removeAttribute('data-src'); };
    if (!window.IntersectionObserver) { imgs.forEach(load); return; }
    if (!this._thumbIo) {
      this._thumbRoot = list;
      this._thumbIo = new IntersectionObserver((es) => es.forEach((e) => {
        if (e.isIntersecting && e.target.dataset.src) { load(e.target); this._thumbIo.unobserve(e.target); }
      }), { root: list, rootMargin: '40px' });
    }
    imgs.forEach(im => this._thumbIo.observe(im));
  }

  // ---- record ----
  // A clip is being recorded: by this card's action, or by anyone (the camera's state)
  _recording() {
    const st = this._st(this._config.entity);
    return !!this._rec || (!!st && st.state === 'recording');
  }

  // Seconds since the recording started: this card's press, else the camera's state change
  _recSeconds() {
    const st = this._st(this._config.entity);
    const t0 = this._rec ? this._rec.at : (st ? Date.parse(st.last_changed) : NaN);
    return isNaN(t0) ? 0 : Math.max(0, Math.floor((Date.now() - t0) / 1000));
  }

  // The integration's record action: it shares a running live view or opens the stream itself (a battery camera
  // wakes) and saves a clip of its Recording length into the history (Captures). Returns when the clip is saved.
  _record() {
    const h = this._hass;
    if (!h || this._recording()) return;
    const R = { at: Date.now() };
    this._rec = R;
    this.render();
    Promise.resolve(h.callService(DOMAIN, 'record', { entity_id: this._config.entity }, undefined, false, true))
      .then((r) => {
        // An entity action answers per entity: { <entity_id>: { complete, ... } }
        const res = r && r.response;
        const body = res && (res[this._config.entity] || res);
        if (body && body.complete === false) this._toast('The clip ended early; the part recorded was saved');
        if (this._open === 'history') this._loadHistory();
      })
      .catch(e => this._toast((e && e.message) || 'Not recorded'))
      .finally(() => { if (this._rec === R) this._rec = null; this.render(); });
  }

  _recTick() {
    if (!this.content || !this._recording()) return;
    const txt = `${this._recSeconds()} s`;
    this.content.querySelectorAll('[data-rect]').forEach((n) => { if (n.textContent !== txt) n.textContent = txt; });
  }

  _today() {
    const tz = this._hass && this._hass.config && this._hass.config.time_zone;
    try { return new Intl.DateTimeFormat('sv-SE', { timeZone: tz || undefined }).format(new Date()); } catch (e) { return new Date().toISOString().slice(0, 10); }
  }

  // ================== LIVE VIEW ==================
  _liveSeconds() {
    const s = parseFloat(this._config.live_seconds);
    return isNaN(s) || s < 10 ? LIVE_S : s;
  }

  // HA defines ha-camera-stream only once a camera card module has loaded; a picture-entity card element
  // (never attached, no hass) imports it without opening anything
  async _streamReady() {
    if (customElements.get('ha-camera-stream')) return;
    if (window.loadCardHelpers) {
      const helpers = await window.loadCardHelpers();
      helpers.createCardElement({ type: 'picture-entity', entity: this._config.entity, camera_view: 'auto' });
    }
    await Promise.race([customElements.whenDefined('ha-camera-stream'), new Promise((_, rej) => setTimeout(() => rej(new Error('ha-camera-stream')), 10000))]);
  }

  // Whether the camera offers a live stream (CameraEntityFeature.STREAM): only then live view and record
  _canLive(st) { return !!st && ((st.attributes.supported_features || 0) & 2) === 2; }

  // `auto_live`: true | 'timed' (a timed view) or 'continuous', started when the card is shown
  _autoMode() {
    const a = this._config && this._config.auto_live;
    if (a === true || a === 'timed') return 'timed';
    return a === 'continuous' ? 'continuous' : null;
  }

  // Once per showing: the card is on screen in a visible tab, not an editor preview, and the camera is available
  _autoTry() {
    const mode = this._autoMode();
    if (!mode || this._autoDone || this._live || !this.isConnected || this.preview || !this._hass || !this.content) return;
    if (document.visibilityState !== 'visible') return;
    const st = this._st(this._config.entity);
    if (!st || st.state === 'unavailable' || !this._canLive(st)) return;
    this._autoDone = true;
    this._start(mode === 'continuous');
  }

  _start(continuous) {
    const h = this._hass, st = this._st(this._config.entity);
    if (!h || !st || st.state === 'unavailable' || !this._canLive(st)) return;
    if (this._live) { this._live.continuous = continuous; this._live.until = continuous ? null : Date.now() + this._liveSeconds() * 1000; this.render(); return; }
    this._view = null;
    this._live = { continuous, until: continuous ? null : Date.now() + this._liveSeconds() * 1000, started: Date.now(), video: false, muted: true };
    const L = this._live;
    this._tickT = setInterval(() => this._tick(), 1000);
    this.render();
    Promise.all([this._streamReady(), this._streamTypes()]).then(([, types]) => {
      if (this._live !== L) return;
      this._attachPlayer(L, this._playerFor(types));
    }).catch(() => { if (this._live === L) this._stop('Live view not available here'); });
  }

  // WebRTC when the browser can receive H.265 over it (go2rtc passes the camera's HEVC through; without it the
  // video track is inactive and the player reports nothing, e.g. Firefox); else HLS, which plays HEVC via MSE.
  _playerFor(types) {
    const rtc = types.includes('web_rtc'), hls = types.includes('hls');
    if (rtc && (!hls || this._rtcHevc())) return 'web_rtc';
    if (hls) return 'hls';
    return 'auto';
  }

  _rtcHevc() {
    try {
      const c = window.RTCRtpReceiver && RTCRtpReceiver.getCapabilities && RTCRtpReceiver.getCapabilities('video');
      return !!c && c.codecs.some(x => /^video\/h265$/i.test(x.mimeType));
    } catch (e) { return false; }
  }

  // The camera's frontend stream types (camera/capabilities), once per card
  _streamTypes() {
    if (!this._types) {
      this._types = this._hass.callWS({ type: 'camera/capabilities', entity_id: this._config.entity })
        .then(r => (r && r.frontend_stream_types) || [])
        .catch(() => { this._types = null; return []; });
    }
    return this._types;
  }

  // One player, one camera consumer: 'web_rtc' or 'hls' alone ('hls': HA's first master playlist waits for a
  // full segment, ~11 s from a cold camera). 'auto': HA's combined player, which runs both at once.
  _attachPlayer(L, kind) {
    if (this._stream) { this._stream.remove(); this._stream = null; }
    let el;
    if (kind === 'web_rtc' || kind === 'hls') {
      el = document.createElement(kind === 'web_rtc' ? 'ha-web-rtc-player' : 'ha-hls-player');
      el.entityid = this._config.entity;
      el.autoPlay = true;
      el.playsInline = true;
    } else {
      el = document.createElement('ha-camera-stream');
      el.hass = this._hass;
      el.stateObj = this._st(this._config.entity);
    }
    // HA's WebRTC player keeps the audio track only when it is unmuted as the track arrives: it starts unmuted
    // and the card mutes its <video> (_applyMute)
    el.muted = kind === 'web_rtc' ? false : L.muted;
    el.fitMode = this._fit();
    L.player = kind;
    clearTimeout(L.stallT);
    if (kind === 'hls') {
      L.stallT = setTimeout(() => {
        if (this._live === L && !L.video && L.player === 'hls') { L.retries = (L.retries || 0) + 1; this._attachPlayer(L, 'hls'); }
      }, HLS_STALL_MS);
    }
    this._stream = el;
    this._vslot.appendChild(el);
    if (kind === 'web_rtc') Promise.resolve(el.updateComplete).then(() => { if (this._stream === el) this._applyMute(); });
  }

  // The live view's mute state on the player: the WebRTC player's own <video> (the player stays unmuted, see
  // _attachPlayer), the player's `muted` otherwise
  _applyMute() {
    const L = this._live, el = this._stream;
    if (!L || !el) return;
    if (L.player !== 'web_rtc') { el.muted = L.muted; return; }
    const v = el.shadowRoot && el.shadowRoot.querySelector('video');
    if (v && v.muted !== L.muted) v.muted = L.muted;
  }

  // reason: toast text ('' for none)
  _stop(reason) {
    if (!this._live && !this._stream) return;
    if (this._live) { clearTimeout(this._live.retryT); clearTimeout(this._live.stallT); }
    clearInterval(this._tickT);
    this._tickT = null;
    if (this._stream) { this._stream.remove(); this._stream = null; }
    this._live = null;
    this._moves = this._moves.slice(0, 1);
    this._atPreset = null;
    this._preOpen = false;
    this._awake(false);
    if (reason) this._toast(reason);
    if (this.content) this.render();
  }

  // Once a second while live: the countdown in place, the end, the start timeout
  _tick() {
    const L = this._live;
    if (!L) return;
    const now = Date.now();
    if (L.until && now >= L.until) { this._stop(''); return; }
    if (!L.video && now - L.started > START_TIMEOUT_MS) { this._stop('No picture from the camera'); return; }
    const txt = L.until ? mmss(Math.ceil((L.until - now) / 1000)) : '∞';
    this.content.querySelectorAll('[data-tick]').forEach((n) => { if (n.textContent !== txt) n.textContent = txt; });
    const ws = `${Math.floor((now - L.started) / 1000)} s`;
    this.content.querySelectorAll('[data-wake]').forEach((n) => { if (n.textContent !== ws) n.textContent = ws; });
  }

  // A hidden tab ends a timed live view; a continuous one keeps running (a wall display).
  // An auto-live card starts again when the tab is shown again.
  _visibility() {
    if (document.visibilityState === 'hidden') {
      if (this._live && !this._live.continuous) this._stop('');
      if (!this._live) this._autoDone = false;
    } else this._autoTry();
  }

  // `streams` from HA's players: { hasVideo, hasAudio }; the first hasVideo ends the start state
  _onStreams(ev) {
    const L = this._live;
    if (!L || !ev.detail) return;
    this._applyMute();  // a player that rendered a new <video> (after an error) starts unmuted
    if (ev.detail.hasAudio) L.audio = true;
    if (ev.detail.hasVideo && !L.video) { L.video = true; this.render(); this._awake(true); return; }
    // The lone WebRTC player reports no video only when it failed (go2rtc error): try HLS once
    if (ev.detail.hasVideo === false && !L.video && L.player === 'web_rtc') { this._attachPlayer(L, 'hls'); return; }
    // HLS reports no video when its first playlist request failed and does not retry: ask again; HA's stream
    // is warm by then and answers at once
    if (ev.detail.hasVideo === false && !L.video && L.player === 'hls') {
      // Doubling waits (1, 2, 4 ... s): a camera the HomeBase cannot serve now fails every ask alike
      clearTimeout(L.retryT);
      L.retryT = setTimeout(() => { if (this._live === L && !L.video) { L.retries = (L.retries || 0) + 1; this._attachPlayer(L, 'hls'); } },
        HLS_RETRY_MS * 2 ** Math.min(L.retries || 0, 4));
    }
  }

  // The controls over the live picture: shown now; with `run`, hidden again CTL_HIDE_MS later while the live
  // picture shows. CSS hides them (class quiet) unless keyboard focus is inside or a mouse rests on a control.
  _awake(run) {
    clearTimeout(this._quietT);
    this._quietT = null;
    if (this._pic) this._pic.classList.remove('quiet');
    if (run && this._live && this._live.video) this._quietT = setTimeout(() => this._hush(), CTL_HIDE_MS);
  }

  // The live controls are faded out now (class quiet, no keyboard focus or hovered control keeping them)
  _isQuiet() {
    const bar = this._pic && this._pic.classList.contains('quiet') && this.content.querySelector('.lbar');
    return !!bar && getComputedStyle(bar).pointerEvents === 'none';
  }

  _hush() {
    clearTimeout(this._quietT);
    this._quietT = null;
    if (this._pic && this._live && this._live.video && !this._preOpen) this._pic.classList.add('quiet');
  }

  _toast(text) {
    this._toastText = text;
    clearTimeout(this._toastT);
    this._toastT = setTimeout(() => { this._toastText = ''; this.render(); }, TOAST_MS);
    if (this.content) this.render();
  }

  // ================== VIEW CONTROLS (immediate) ==================
  // Pan/tilt steps and go-to share one lane: the camera turns one move at a time, and a step's call returns
  // only after the move settled (~1.5 s): a step sent into a moving camera is cut short. Presses while one runs
  // wait in order, up to MOVE_QUEUE.
  _move(key, service, data) {
    if (!this._hass || this._moves.length > MOVE_QUEUE) return;
    this._moves.push({ key, service, data });
    if (this._moves.length === 1) this._runMove(); else this.render();
  }

  _runMove() {
    const m = this._moves[0];
    if (!m || !this._hass) { this._moves = []; return; }
    if (m.service === 'pan_tilt') this._atPreset = null;
    this.render();
    Promise.resolve(this._hass.callService(DOMAIN, m.service, { entity_id: this._config.entity, ...m.data }))
      .then(() => { this._moves.shift(); if (m.service === 'goto_preset') this._atPreset = m.data.preset; })
      .catch((e) => { this._moves = []; this._toast((e && e.message) || 'Not applied'); })
      .finally(() => { if (this._moves.length && this.isConnected) this._runMove(); else { this._moves = []; this.render(); } });
  }

  // 'run' for the running move, the count of waiting presses, or ''
  _moveState(key) {
    if (!this._moves.length) return { run: false, n: 0 };
    return { run: this._moves[0].key === key, n: this._moves.slice(1).filter(m => m.key === key).length };
  }

  // dir: 'in' | 'out' one step (the integration's zoom action), 'reset' to the minimum (the zoom number)
  _zoom(dir) {
    const E = this._entities(), h = this._hass, st = this._st(E.zoom);
    if (!h || !st || Object.keys(this._busy).some(k => k.startsWith('zoom'))) return;
    const min = parseFloat(st.attributes.min) || 1, max = parseFloat(st.attributes.max) || 12;
    const cur = num(st);
    if (dir === 'reset') {
      this._zoomTarget = min;
      this._ptzCall('zoom-reset', h.callService('number', 'set_value', { entity_id: E.zoom, value: min }));
      return;
    }
    this._zoomTarget = Math.min(max, Math.max(min, (isNaN(cur) ? min : cur) + (dir === 'in' ? 1 : -1)));
    this._ptz(`zoom-${dir}`, 'zoom', { direction: dir });
  }

  _ptzCall(key, p) {
    this._busy[key] = true;
    this.render();
    Promise.resolve(p).catch((e) => this._toast((e && e.message) || 'Not applied'))
      .finally(() => { delete this._busy[key]; this._zoomTarget = undefined; this.render(); });
  }

  // Zoom is answered at the camera's receipt (~0.1 s)
  _ptz(key, service, data) {
    const h = this._hass;
    if (!h || this._busy[key]) return;
    this._ptzCall(key, h.callService(DOMAIN, service, { entity_id: this._config.entity, ...data }));
  }

  // ================== PENDING ==================
  _settle(key, live, confirmed) {
    const p = this._pending[key];
    if (!p) return { value: live, status: '' };
    if (!p.at) return { value: p.value, status: 'pending' };
    if (confirmed) { delete this._pending[key]; return { value: live, status: '' }; }
    const age = Date.now() - p.at, lim = key === 'act' ? ACT_PENDING_MS : PENDING_MS;
    if (age <= lim) return { value: p.value, status: 'pending' };
    if (age <= lim + FAILED_MS) return { value: live, status: p.unsure ? 'unsure' : 'failed', sent: p.value };
    delete this._pending[key];
    return { value: live, status: '' };
  }

  _arm() {
    clearTimeout(this._timer);
    this._timer = null;
    if (!this.isConnected) return;
    const now = Date.now();
    const due = [];
    Object.entries(this._pending).forEach(([k, p]) => {
      const lim = k === 'act' ? ACT_PENDING_MS : PENDING_MS;
      if (p.at) due.push(p.at + lim + 50, p.at + lim + FAILED_MS + 50);
    });
    const next = Math.min(...due.filter(t => t > now));
    if (isFinite(next)) this._timer = setTimeout(() => { this._timer = null; this.render(); }, next - now);
  }

  // ================== EVENTS ==================
  _onClick(ev) {
    const path = ev.composedPath();
    const has = (k) => path.find(n => n.dataset && n.dataset[k] !== undefined);
    if (has('menu')) {
      this._setMenu(!this._menuOpen);
      this._focus(this._menuOpen ? 'mi-0' : 'menu');
      return;
    }
    if (this._menuOpen && !path.find(n => n.classList && n.classList.contains('menu'))) this._setMenu(false);
    const dd = has('dd');
    if (dd) {
      const k = dd.dataset.dd;
      this._setDd(this._dd === k ? null : k);
      this._focus(this._dd ? `ddo-${k}-${this._ddFocusOpt(k)}` : `dd-${k}`);
      return;
    }
    if (this._dd && !path.find(n => n.classList && n.classList.contains('ddl'))) this._setDd(null);
    const lv = has('live');
    if (lv) {
      const v = lv.dataset.live;
      if (v === 'rec') {
        if (this._menuOpen) this._setMenu(false);
        this._record();
        this._focus(this._live ? 'live-rec' : 'play');
        return;
      }
      if (v === 'stop') this._stop('');
      else if (v === 'mute') { if (this._live) { this._live.muted = !this._live.muted; this._applyMute(); this.render(); } }
      else if (v === 'full') this._fullscreen();
      else if (v === 'presets') { this._preOpen = !this._preOpen; this.render(); this._focus(this._preOpen ? `lp-${this._firstPreset()}` : 'live-presets'); return; }
      else if (v === 'pin') this._start(!(this._live && this._live.continuous));
      else this._start(v === 'continuous');
      if (this._menuOpen) this._setMenu(false);
      this._focus(v === 'stop' || !this._live ? 'play' : `live-${v}`);
      return;
    }
    const act = has('action');
    if (act) {
      const v = act.dataset.action;
      if (this._stg.act === v) delete this._stg.act; else this._stg.act = v;
      this._setMenu(false);
      this._focus('menu');
      return;
    }
    const pt = has('pt');
    if (pt) { this._move(`pt-${pt.dataset.pt}`, 'pan_tilt', { direction: pt.dataset.pt }); return; }
    const zm = has('zoom');
    if (zm) { this._zoom(zm.dataset.zoom); return; }
    const gp = has('goto');
    if (gp) { this._move(`goto-${gp.dataset.goto}`, 'goto_preset', { preset: parseInt(gp.dataset.goto, 10) }); return; }
    const rp = has('rplay') || has('rsave');
    if (rp) {
      const save = rp.dataset.rsave !== undefined;
      this._fetchRecording(parseInt(save ? rp.dataset.rsave : rp.dataset.rplay, 10), !save);
      return;
    }
    if (has('srefresh')) { this._loadStation(); return; }
    if (has('smore')) { this._loadStation(true); return; }
    if (has('hmore')) {
      // The next tiles go after the shown ones; focus moves to the first of them
      const n0 = this._hn;
      this._hn += HISTORY_PAGE;
      this.render();
      const t = this.content.querySelectorAll('.hstrip .ht')[n0];
      if (t) t.focus({ preventScroll: true }); else this._focus('hmore');
      return;
    }
    const hv = has('hist');
    if (hv) {
      const id = hv.dataset.hist;
      this._view = this._view === id ? null : id;
      this.render();
      this._focus(this._view ? 'hist-close' : `hist-${id}`);
      return;
    }
    const htab = has('htab');
    if (htab) {
      if (this._htab !== htab.dataset.htab) this._hn = HISTORY_PAGE;
      this._htab = htab.dataset.htab;
      if (this._htab === 'station') this._loadStation();
      this.render();
      this._focus(`htab-${this._htab}`);
      return;
    }
    if (has('hday')) { this._pickDay(has('hday')); return; }
    if (has('hdayclear')) { this._setDay(null); this._focus('hday'); return; }
    const hf = has('hf');
    if (hf) {
      const t = this._htab, k = hf.dataset.hf;
      const f = { pic: true, vid: true, ...(this._hfilt[t] || {}) };
      // At least one kind stays shown: the last one on is not turned off
      if (!(f[k] && !f[k === 'pic' ? 'vid' : 'pic'])) f[k] = !f[k];
      this._hfilt[t] = f;
      this.render();
      this._focus(`hf-${k}`);
      return;
    }
    if (has('histclose')) {
      const id = this._view;
      this._view = null;
      this.render();
      this._focus(this._open === 'history' && id ? `hist-${id}` : 'play');
      return;
    }
    const more = has('more');
    if (more) {
      const id = more.dataset.more;
      this._open = this._open === id ? null : id;
      if (this._open === 'history') {
        this._hn = HISTORY_PAGE;
        this._hday = null;
        dateInputReady();
        // Station shows unless the integration said the camera has no station recordings
        this._stationTab = STATION_SUPPORT.get(this._config.entity) !== false;
        this._loadHistory();
        if (this._htab === 'station' && this._stationTab) this._loadStation();
      } else this._view = null;
      this.render();
      this._focus(`more-${id}`);
      return;
    }
    const stab = has('stab');
    if (stab) {
      this._tab = stab.dataset.stab;
      this._tabPicked = true;
      this._dd = null;
      this.render();
      this._focus(`stab-${this._tab}`);
      return;
    }
    const opt = has('opt');
    if (opt) {
      const [kind, v] = [opt.dataset.kind, opt.dataset.opt];
      const live = this._ctx && this._ctx[kind];
      if (live !== undefined && String(live) === v) delete this._stg[kind]; else this._stg[kind] = v;
      const fromDd = this._dd === kind;
      if (fromDd) this._setDd(null); else this.render();
      this._focus(fromDd ? `dd-${kind}` : `opt-${kind}-${v}`);
      return;
    }
    const tg = has('tg');
    if (tg) {
      const k = tg.dataset.tg;
      const live = this._ctx && this._ctx[k];
      const cur = this._stg[k] !== undefined ? this._stg[k] : live;
      const next = cur === 'on' ? 'off' : 'on';
      if (live !== undefined && String(live) === next) delete this._stg[k]; else this._stg[k] = next;
      this.render();
      this._focus(`tg-${k}`);
      return;
    }
    const rs = has('restore');
    if (rs) {
      delete this._stg[rs.dataset.restore];
      this.render();
      this._focus(`range-${rs.dataset.restore}`);
      return;
    }
    const info = has('info');
    if (info) {
      this.dispatchEvent(new CustomEvent('hass-more-info', { detail: { entityId: info.dataset.info }, bubbles: true, composed: true }));
      return;
    }
    const a = has('act');
    if (a) {
      if (a.dataset.act === 'set') this._commit(); else { this._stg = {}; this.render(); }
      this._focus('menu');
    }
  }

  _onKey(ev) {
    // History sub-tabs, Settings tabs and section tabs: arrows, Home and End move between them (ARIA tabs)
    const t = this.shadowRoot.activeElement;
    if (t && t.dataset && t.dataset.htab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(ev.key)) {
      const ids = [...this.content.querySelectorAll('.htabs .stab')].map(b => b.dataset.htab);
      const i = ids.indexOf(t.dataset.htab);
      const j = ev.key === 'Home' ? 0 : (ev.key === 'End' ? ids.length - 1 : (i + (ev.key === 'ArrowRight' ? 1 : -1) + ids.length) % ids.length);
      ev.preventDefault();
      if (this._htab !== ids[j]) this._hn = HISTORY_PAGE;
      this._htab = ids[j];
      if (this._htab === 'station') this._loadStation();
      this.render();
      this._focus(`htab-${this._htab}`);
      return;
    }
    if (t && t.dataset && t.dataset.stab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(ev.key)) {
      const ids = [...this.content.querySelectorAll('.stab')].map(b => b.dataset.stab);
      const i = ids.indexOf(t.dataset.stab);
      const j = ev.key === 'Home' ? 0 : (ev.key === 'End' ? ids.length - 1 : (i + (ev.key === 'ArrowRight' ? 1 : -1) + ids.length) % ids.length);
      ev.preventDefault();
      this._tab = ids[j];
      this._tabPicked = true;
      this.render();
      this._focus(`stab-${this._tab}`);
      return;
    }
    if (t && t.classList && t.classList.contains('sec') && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(ev.key)) {
      const bs = [...this.content.querySelectorAll('.secs .sec')];
      const i = bs.indexOf(t);
      const j = ev.key === 'Home' ? 0 : (ev.key === 'End' ? bs.length - 1 : (i + (ev.key === 'ArrowRight' ? 1 : -1) + bs.length) % bs.length);
      ev.preventDefault();
      bs[j].focus();
      return;
    }
    if (this._menuOpen && ev.key === 'Escape') { ev.preventDefault(); this._setMenu(false); this._focus('menu'); return; }
    if (this._dd && ev.key === 'Escape') { ev.preventDefault(); const k = this._dd; this._setDd(null); this._focus(`dd-${k}`); return; }
    if (this._dd && (ev.key === 'ArrowDown' || ev.key === 'ArrowUp')) {
      const items = [...this.content.querySelectorAll('.ddl .ddo')];
      const at = items.indexOf(this.shadowRoot.activeElement);
      const nx = items[at < 0 ? 0 : (at + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length];
      if (nx) { ev.preventDefault(); nx.focus(); }
      return;
    }
    if (this._preOpen && ev.key === 'Escape' && !this._isFull()) { ev.preventDefault(); this._preOpen = false; this.render(); this._focus('live-presets'); return; }
    if (this._menuOpen && (ev.key === 'ArrowDown' || ev.key === 'ArrowUp')) {
      const items = [...this.content.querySelectorAll('.menu .mi:not(:disabled)')];
      const at = items.indexOf(this.shadowRoot.activeElement);
      const nx = items[(at + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length];
      if (nx) { ev.preventDefault(); nx.focus(); }
      return;
    }
    if (ev.key === 'Escape' && this._view) { ev.preventDefault(); this._view = null; this.render(); this._focus('more-history'); return; }
    if (ev.key === 'Escape' && Object.keys(this._stg).length) { ev.preventDefault(); this._stg = {}; this.render(); }
  }

  // The card's 16:9 frame is filled edge to edge; full screen shows the whole picture (a screen of another
  // shape, a phone held upright) with bars instead of cutting its sides
  _fit() { return this._isFull() ? 'contain' : 'cover'; }

  // The picture is in full screen. document.fullscreenElement names the card host (shadow retargeting), so the
  // card's own root is asked.
  _isFull() { return !!this._pic && this.shadowRoot.fullscreenElement === this._pic; }

  _firstPreset() {
    const E = this._entities();
    return [...Object.keys(E.presetImages), ...Object.keys(E.presetButtons)].map(Number).sort((a, b) => a - b)[0];
  }

  _fullscreen() {
    const pic = this.content.querySelector('.pic');
    if (this._isFull()) { document.exitFullscreen().catch(() => {}); return; }
    if (pic && pic.requestFullscreen) pic.requestFullscreen().catch(() => {});
  }

  _setMenu(open) { this._menuOpen = open; this._watchOutside(open || !!this._dd); this.render(); }

  // One settings dropdown open at a time (key or null)
  _setDd(key) { this._dd = key; this._watchOutside(!!key || !!this._menuOpen); this.render(); }

  // The option to focus when a dropdown opens: the staged or current one
  _ddFocusOpt(k) {
    const r = this._ctx && this._ctx.rows && this._ctx.rows[k];
    const v = this._stg[k] !== undefined ? this._stg[k] : (this._ctx ? this._ctx[k] : undefined);
    return r && v !== undefined ? String(v) : '';
  }

  // A press outside the card closes the menu and the dropdown
  _watchOutside(on) {
    if (this._outside) { document.removeEventListener('pointerdown', this._outside, true); this._outside = null; }
    if (!on) return;
    this._outside = (ev) => {
      if (ev.composedPath().includes(this)) return;
      this._menuOpen = false; this._dd = null; this._watchOutside(false); this.render();
    };
    document.addEventListener('pointerdown', this._outside, true);
  }

  // Opens the dropdown list downward, or upward when the card has more room above (ha-card clips its content);
  // a list longer than either side scrolls
  _placeDd() {
    const l = this._dd && this.content.querySelector('.ddl');
    if (!l) return;
    l.classList.remove('up');
    l.style.maxHeight = '';
    const cr = this.content.getBoundingClientRect(), br = l.parentElement.getBoundingClientRect();
    const h = l.scrollHeight, below = cr.bottom - br.bottom - 6, above = br.top - cr.top - 6;
    if (h <= below) return;
    if (above > below) { l.classList.add('up'); if (h > above) l.style.maxHeight = `${Math.max(64, Math.floor(above))}px`; }
    else l.style.maxHeight = `${Math.max(64, Math.floor(below))}px`;
  }

  _focus(key) {
    const el = this.content && this.content.querySelector(`[data-focus="${CSS.escape(key)}"]`);
    if (el && !el.disabled && !el.closest('[hidden], [inert]')) el.focus({ preventScroll: true });
  }

  // A slider moved: stage the snapped value and patch its row in place (a re-render would end the drag)
  _stageRange(r) {
    const kind = r.dataset.kind;
    const c = this._ctx && this._ctx.ranges && this._ctx.ranges[kind];
    if (!c) return;
    const v = Math.min(c.max, Math.max(c.min, Math.round(parseFloat(r.value) / c.step) * c.step));
    // A select shown as a slider stages the option at the index
    if (!isNaN(c.value) && Math.abs(v - c.value) < 1e-6) delete this._stg[kind]; else this._stg[kind] = c.opts ? c.opts[v] : v;
    r.style.setProperty('--pos', `${(((v - c.min) / (c.max - c.min)) * 100).toFixed(1)}%`);
    r.setAttribute('aria-valuetext', c.fmt(v));
    const row = r.closest('.row');
    if (row) {
      row.classList.toggle('staged', this._stg[kind] !== undefined);
      const rv = row.querySelector('.rval');
      if (rv) rv.textContent = c.fmt(v);
    }
    this._syncActs();
  }

  _syncActs() {
    const n = Object.keys(this._stg).length;
    const hdr = this.content.querySelector('.hdr');
    if (hdr) hdr.classList.toggle('staging', !!n);
    const acts = this.content.querySelector('.acts');
    if (!acts) return;
    acts.classList.toggle('off', !n);
    acts.inert = !n;
    if (n) acts.removeAttribute('aria-hidden'); else acts.setAttribute('aria-hidden', 'true');
    const set = acts.querySelector('[data-act="set"]');
    set.setAttribute('aria-label', `Set: ${n ? this._hint() : ''}`);
    set.classList.toggle('warn', n > 0 && this._restartWarn());
    const cnt = set.querySelector('.cnt');
    cnt.textContent = String(n > 1 ? n : 0);
    cnt.classList.toggle('none', n <= 1);
  }

  // A streaming-quality write ends a running live view (the integration restarts it at the new size)
  _restartWarn() { return !!this._live && Object.keys(this._stg).some(k => RESTARTS_LIVE.test(k)); }

  // What Set will send, in send order (Set's aria-label)
  _hint() {
    const s = this._stg, c = this._ctx || {};
    const parts = [];
    (c.order || []).forEach((k) => {
      if (s[k] === undefined) return;
      const r = c.rows && c.rows[k];
      parts.push(`${r ? r.label : k} ${r ? r.fmtLive : ''} → ${r ? r.fmt(s[k]) : s[k]}`);
    });
    if (s.act) parts.push(ACTIONS[s.act].label);
    let t = parts.join(' · ');
    if (this._restartWarn()) t += ' ⚠ Changing the streaming quality restarts the live view.';
    return t;
  }

  // Set: settings in row order, then the action. Each write returns after the station's read-back.
  _commit() {
    const s = this._stg, E = this._entities(), h = this._hass;
    if (!h || !Object.keys(s).length) return;
    const items = [];
    (this._ctx.order || []).forEach((k) => {
      const id = E.rowIds[k];
      if (s[k] === undefined || !id) return;
      const dom = id.split('.')[0];
      let call;
      if (dom === 'select') call = ['select', 'select_option', { entity_id: id, option: String(s[k]) }];
      else if (dom === 'switch') call = ['switch', s[k] === 'on' ? 'turn_on' : 'turn_off', { entity_id: id }];
      else call = ['number', 'set_value', { entity_id: id, value: s[k] }];
      items.push({ key: k, value: s[k], call, id, waits: !!this._dependsOn(id, k) });
    });
    if (s.act && E[s.act]) items.push({ key: 'act', value: s.act, call: ['button', 'press', { entity_id: E[s.act] }] });
    this._stg = {};
    // One after another: the station handles one command at a time
    const run = (i) => {
      if (i >= items.length || !this._hass) return;
      const it = items[i];
      this._pending[it.key] = { value: it.value, at: Date.now(), was: it.key === 'act' ? this._actStamp(it.value) : undefined };
      this._arm();
      this.render();
      // HA skips a call to an unavailable entity: a dependent is sent once its controller's write made it available
      const ready = it.waits ? this._whenAvailable(it.id, PENDING_MS) : Promise.resolve(true);
      ready.then((up) => {
        if (!up) return Promise.reject(new Error(`${this._ctx.rows[it.key] ? this._ctx.rows[it.key].label : it.key}: not available`));
        return this._hass.callService(...it.call);
      }).catch((e) => {
        const p = this._pending[it.key];
        // A write that timed out may still apply: the row says so instead of "Not applied"
        if (p && e && e.translation_key === 'setting_unconfirmed') { p.unsure = true; p.at = Date.now() - PENDING_MS - 1; }
        else if (p) p.at = Date.now() - PENDING_MS - 1;
        if (e && e.message) this._toast(e.message);
        this.render();
      }).finally(() => run(i + 1));
    };
    items.forEach((it) => { this._pending[it.key] = { value: it.value, at: 0 }; });
    run(0);
    this.render();
  }

  // Resolves true once the entity's state is no longer unavailable, false after `ms`
  _whenAvailable(id, ms) {
    const up = () => { const x = this._st(id); return !!x && x.state !== 'unavailable'; };
    if (up()) return Promise.resolve(true);
    return new Promise((res) => {
      const t0 = Date.now();
      const iv = setInterval(() => {
        if (up()) { clearInterval(iv); res(true); } else if (Date.now() - t0 > ms || !this.isConnected) { clearInterval(iv); res(false); }
      }, 200);
    });
  }

  // A button's state is its last press time; a press is confirmed when it changes
  _actStamp(a) { const st = this._st(this._entities()[a]); return st ? st.state : ''; }

  // ================== RENDER ==================
  _ago(ms) {
    const s = (Date.now() - ms) / 1000;
    if (isNaN(s)) return '';
    if (s < 60) return 'now';
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }

  _name(st) {
    const n = this._config.name;
    if (typeof n === 'string' && n.trim()) return n.trim();
    if (n && typeof n === 'object' && st && typeof this._hass.formatEntityName === 'function') {
      try { const r = this._hass.formatEntityName(st, Array.isArray(n) ? n : [n]); if (r) return r; } catch (e) { /* default below */ }
    }
    const d = this._entities().device;
    return (st && st.attributes.friendly_name) || (d && (d.name_by_user || d.name)) || 'Camera';
  }

  // Option text via the integration's translations (a library-named setting's options are already text)
  _optLabel(stObj, v) {
    if (this._hass.formatEntityState && stObj) { try { const r = this._hass.formatEntityState(stObj, v); if (r) return r; } catch (e) { /* below */ } }
    return human(v);
  }

  // A settings row's name: the entity's own name without the device name
  _rowLabel(id, key, st) {
    if (ROW_LABELS[key]) return ROW_LABELS[key];
    const h = this._hass;
    if (st && typeof h.formatEntityName === 'function') {
      try { const r = h.formatEntityName(st, [{ type: 'entity' }]); if (r) return sentence(r); } catch (e) { /* below */ }
    }
    const d = this._entities().device;
    const dn = d && (d.name_by_user || d.name);
    let n = (st && st.attributes.friendly_name) || human(key);
    if (dn && n.startsWith(`${dn} `)) n = n.slice(dn.length + 1);
    return sentence(n.charAt(0).toUpperCase() + n.slice(1));
  }

  // A row's group: the GROUPS entry naming its key; a key added by `settings_include` -> Other; else More settings
  _group(key, inc) {
    const g = GROUPS.find(x => x[3].includes(key));
    if (g) return { id: g[0], at: g[3].indexOf(key) };
    return inc ? { id: 'other', at: 99 } : { id: 'more', at: 99 };
  }

  // The setting a row depends on: { key, id, opt } with opt the controller's option it needs (null when unknown).
  // Sources in order: a controller's `controls`, the row's `applies_when_label` / `applies_when` (last seen).
  _dependsOn(id, key) {
    const E = this._entities();
    for (const c of E.rows) {
      const m = (this._st(c.id) || { attributes: {} }).attributes.controls;
      if (m && typeof m === 'object' && m[key] !== undefined) return { key: c.key, id: c.id, opt: String(m[key]) };
    }
    const a = APPLIES.get(id);
    const [k, v] = a ? String(a).split('=') : [];
    if (!k) return null;
    const cid = E.byKey[k];
    const cSt = this._st(cid);
    if (!cid || !cSt) return null;
    const opts = Array.isArray(cSt.attributes.options) ? cSt.attributes.options : [];
    const lbl = LABELS.get(id);
    const opt = opts.includes(v) ? v : (lbl && opts.includes(lbl) ? lbl : null);
    return { key: k, id: cid, opt };
  }

  // A row's state shown while it is unavailable: the last known one
  _known(id, sSt) {
    if (ok(sSt)) { LAST.set(id, sSt.state); return sSt.state; }
    return sSt && sSt.state === 'unavailable' ? LAST.get(id) : undefined;
  }

  render() {
    if (!this._config || !this._hass) return;
    if (!this.content) this._build();
    const h = this._hass;
    const E = this._entities();
    const st = this._st(E.camera);
    const stg = this._stg;
    const ctx = { ranges: {}, rows: {}, order: [] };
    const avail = !!st && st.state !== 'unavailable';
    const canLive = this._canLive(st);
    if (!avail && this._live) { this._stop('Camera unavailable'); return; }
    const L = this._live;
    const recording = avail && this._recording();
    if (recording && !this._recT) this._recT = setInterval(() => this._recTick(), 1000);
    if (!recording && this._recT) { clearInterval(this._recT); this._recT = null; }

    // ---- status ----
    const bat = num(this._st(E.battery));
    const active = DETECT.find(([k]) => { const s = this._st(E[k]); return s && s.state === 'on'; });
    const evSt = this._st(E.detection);
    const evMs = ok(evSt) ? Date.parse(evSt.state) : NaN;
    const evType = evSt && evSt.attributes.event_type;
    let word, wcls = '';
    if (!avail) { word = 'Unavailable'; wcls = ' err'; }
    else if (L && !L.video) { word = 'Starting live view'; wcls = ' pending'; }
    else if (L) { word = L.continuous ? 'Live · continuous' : 'Live'; wcls = ' live'; }
    else if (recording) { word = 'Recording'; wcls = ' live'; }
    else if (active) { word = active[1]; wcls = ' det'; }
    else word = 'Idle';
    const detail = [];
    if (!L && !active && evType && !isNaN(evMs)) detail.push(`${EVENT_WORD[evType] || human(evType)} ${this._ago(evMs)}`);
    const cls = !avail ? 'off' : (L || recording ? 'live' : (active ? 'det' : 'idle'));

    // ---- action (staged) ----
    const actP = this._pending.act;
    const actSt = this._settle('act', null, !!actP && this._actStamp(actP.value) !== actP.was);
    ['capture', 'refresh'].forEach((a) => { if (stg.act === a && !E[a]) delete stg.act; });

    // ---- settings rows (grouped; row order is the send order) ----
    const rows = [];
    const presetLbl = v => (v === 'camera_default' ? 'Default' : `P${parseInt(v, 10) + 1}`);
    E.rows.forEach(({ id, key, inc }) => {
      const sSt = this._st(id);
      if (!sSt) { delete stg[key]; return; }
      if (sSt.attributes.applies_when) APPLIES.set(id, sSt.attributes.applies_when);
      if (sSt.attributes.applies_when_label) LABELS.set(id, sSt.attributes.applies_when_label);
      const dom = id.split('.')[0];
      // A dependent applies while its controller shows (staged, else live) the option it needs; staging that
      // option makes it editable before the device reports it available
      const dep = this._dependsOn(id, key);
      let applies = true, why = '';
      if (dep) {
        const cv = stg[dep.key] !== undefined ? String(stg[dep.key]) : (this._st(dep.id) || {}).state;
        applies = dep.opt ? cv === dep.opt : sSt.state !== 'unavailable';
        if (!applies && !dep.opt) why = 'unavailable';
      }
      const off = !avail || !applies || (sSt.state === 'unavailable' && !(dep && stg[dep.key] !== undefined));
      if (off) delete stg[key];
      const label = this._rowLabel(id, key, sSt);
      const g = dep ? { id: 'dep', at: 0 } : this._group(key, inc);
      const icon = ROW_ICONS[key] || ROW_ICONS[key.replace(/__v\d+$|_new$/, '')] || (this._hass.entities[id] && this._hass.entities[id].icon) || sSt.attributes.icon
        || (GROUPS.find(x => x[0] === g.id) || GROUPS[GROUPS.length - 1])[2];
      const warn = RESTARTS_LIVE.test(key) && stg[key] !== undefined && !!L;
      const base = { id: key, eid: id, label, icon, group: g, off, warn, dep: dep && dep.key, unrep: !!sSt.attributes.assumed_state && !ok(sSt) && !this._pending[key],
        sub: off ? (avail && !dep ? 'unavailable' : (avail ? why : '')) : (warn ? 'restarts live' : '') };
      // A setting the device never reports (assumed state): its value reads "not reported" until written from here
      const unrep = !!sSt.attributes.assumed_state && !ok(sSt);
      const known = this._known(id, sSt);
      if (dom === 'number') {
        const liveV = known === undefined ? NaN : parseFloat(known);
        const p = this._pending[key];
        const s = this._settle(key, liveV, p && Math.abs(p.value - liveV) < 1e-6);
        const a = sSt.attributes;
        const unit = a.unit_of_measurement || '';
        const c = { value: s.value, min: parseFloat(a.min) || 0, max: parseFloat(a.max) || 100, step: parseFloat(a.step) || 1,
          fmt: v => (isNaN(v) ? (base.unrep ? 'Not reported' : '--') : `${Math.round(v * 10) / 10}${unit ? (unit === '%' || unit === 's' ? ` ${unit}` : unit) : ''}`) };
        ctx.ranges[key] = c;
        ctx[key] = s.value;
        if (stg[key] !== undefined && Math.abs(stg[key] - s.value) < 1e-6) delete stg[key];
        const shown = stg[key] !== undefined ? stg[key] : s.value;
        rows.push({ ...base, st: s, staged: stg[key] !== undefined, value: c.fmt(shown), range: c, shown });
        ctx.rows[key] = { label, fmt: c.fmt, fmtLive: c.fmt(s.value) };
        return;
      }
      const s = this._settle(key, known, this._pending[key] && String(this._pending[key].value) === sSt.state);
      ctx[key] = s.value;
      if (stg[key] !== undefined && String(stg[key]) === String(s.value)) delete stg[key];
      const shown = stg[key] !== undefined ? stg[key] : s.value;
      if (dom === 'switch') {
        // One line: the name and a toggle (staged like every other row)
        const fmtS = v => (v === 'on' ? 'On' : (v === 'off' ? 'Off' : '--'));
        ctx.rows[key] = { label, fmt: fmtS, fmtLive: fmtS(s.value) };
        rows.push({ ...base, st: s, staged: stg[key] !== undefined, value: '', tg: { on: shown === 'on', known: shown === 'on' || shown === 'off' } });
        return;
      }
      let opts, lbl;
      {
        opts = (Array.isArray(sSt.attributes.options) ? sSt.attributes.options : []).filter(v => !/^undocumented/.test(v) || v === s.value);
        lbl = v => (PRESET_KEYS.includes(key) ? presetLbl(v) : this._optLabel(sSt, v));
      }
      const fmt = v => (v === undefined ? (unrep ? 'Not reported' : 'Unknown') : lbl(String(v)));
      const short = v => (PRESET_KEYS.includes(key) ? presetLbl(v) : (OPT_SHORT[v] || lbl(v)));
      const r = { ...base, st: s, staged: stg[key] !== undefined };
      ctx.rows[key] = { label, fmt, fmtLive: fmt(s.value) };
      const numeric = dom === 'select' && !PRESET_KEYS.includes(key) && opts.length > 2 && opts.every(v => /^-?\d+(\.\d+)?$/.test(v));
      if (numeric) {
        // Numbered choices (a sensitivity 1-7) as a stepped slider over the options in numeric order
        const o = [...opts].sort((x, y) => parseFloat(x) - parseFloat(y));
        const c = { opts: o, value: o.indexOf(String(s.value)), min: 0, max: o.length - 1, step: 1, fmt: i => (o[i] === undefined ? (base.unrep ? 'Not reported' : '--') : lbl(o[i])) };
        if (c.value < 0) c.value = NaN;
        ctx.ranges[key] = c;
        const si = o.indexOf(String(shown));
        rows.push({ ...r, value: fmt(shown), range: c, shown: si < 0 ? NaN : si });
      } else if (opts.length <= SEG_MAX && opts.every(v => short(v).length <= SEG_CHARS)) {
        rows.push({ ...r, value: shown === undefined ? (unrep ? 'Not reported' : '--') : short(String(shown)), segLong: opts.some(v => short(v).length > 8), segIcons: opts.every(v => !!OPT_ICONS[v]),
          seg: opts.map(v => ({ v, l: short(v), full: lbl(v), i: OPT_ICONS[v], on: String(shown) === v })) });
      } else {
        rows.push({ ...r, value: '', dd: { shown: fmt(shown), open: this._dd === key && !off,
          opts: opts.map(v => ({ v, l: lbl(v), on: String(shown) === v, live: String(s.value) === v })) } });
      }
    });
    // Within a group the two-line rows come first and the one-line toggles after, so rows side by side match in height.
    // Dependents follow their controller (the send order too); More settings by name. Groups in the configured order;
    // a hidden group's rows (and their dependents) are left out, so nothing in them is staged or sent.
    const gOrder = shownGroups(this._config);
    const gi = id => gOrder.indexOf(id);
    const deps = {};
    rows.filter(r => r.dep).forEach((r) => { (deps[r.dep] = deps[r.dep] || []).push(r); });
    const ctlKeys = new Set(rows.map(r => r.id));
    Object.keys(deps).forEach((k) => {
      if (!ctlKeys.has(k)) { deps[k].forEach((r) => { r.dep = null; r.group = this._group(r.id, false); }); delete deps[k]; return; }
      deps[k].sort((a, b) => !!a.tg - !!b.tg || DEP_ORDER.indexOf(a.id) - DEP_ORDER.indexOf(b.id) || a.label.localeCompare(b.label));
    });
    const top = rows.filter(r => !r.dep && gOrder.includes(r.group.id));
    top.sort((a, b) => gi(a.group.id) - gi(b.group.id)
      || !!a.tg - !!b.tg || (a.group.id === 'more' ? a.label.localeCompare(b.label) : (a.group.at - b.group.at || a.label.localeCompare(b.label))));
    rows.length = 0;
    top.forEach((r) => {
      if (deps[r.id]) {
        r.deps = deps[r.id];
        const d = this._dependsOn(r.deps[0].eid, r.deps[0].id);
        r.depOpt = d && d.opt ? (OPT_SHORT[d.opt] || this._optLabel(this._st(d.id), d.opt)) : '';
        r.depOn = !r.deps.every(x => x.off);
      }
      rows.push(r, ...(deps[r.id] || []));
    });
    rows.forEach(r => ctx.order.push(r.id));
    const nMore = top.filter(r => r.group.id === 'more').length;
    // Settings tabs: the shown groups that have rows, in order; the open one stays while it has rows
    const tabs = [...new Set(top.map(r => r.group.id))];
    // Until the user picks a tab the first one is shown (rows found later through the registry may add earlier tabs)
    if (!this._tabPicked || !tabs.includes(this._tab)) this._tab = tabs[0];
    if (this._dd && !rows.some(r => r.dd && r.dd.open)) { this._dd = null; this._watchOutside(!!this._menuOpen); }

    // ---- presets (go to: immediate) ----
    const presetIdx = [...new Set([...Object.keys(E.presetImages), ...Object.keys(E.presetButtons)].map(Number))].sort((x, y) => x - y);
    const zoomSt = this._st(E.zoom);
    const zoom = num(zoomSt);
    const zMin = zoomSt ? parseFloat(zoomSt.attributes.min) || 1 : 1;
    const zMax = zoomSt ? parseFloat(zoomSt.attributes.max) || 12 : 12;
    const defIdx = ok(this._st(E.defaultPreset)) ? parseInt(this._st(E.defaultPreset).state, 10) : NaN;
    this._ctx = ctx;

    // ---- header ----
    const nStaged = Object.keys(stg).length;
    const name = this._name(st);
    const shownWord = stg.act ? `→ ${ACTIONS[stg.act].label}` : (actSt.status === 'pending' ? `${ACTIONS[actSt.value].label} · sending`
      : (actSt.status === 'failed' ? 'Not applied' : word));
    const wordCls = stg.act ? ' staged' : (actSt.status === 'pending' ? ' pending' : (actSt.status === 'failed' ? ' err' : wcls));
    const badge = L || recording ? ['live', 'mdi:record'] : (active ? ['det', active[2]] : null);
    const liveMenu = !avail || !canLive ? [] : (L
      ? [['live', 'pin', L.continuous ? `Stop after ${mmss(this._liveSeconds())}` : 'Keep live (continuous)', L.continuous ? 'mdi:timer-outline' : 'mdi:all-inclusive'],
        ['live', 'full', 'Full screen', 'mdi:fullscreen'], ['live', 'stop', 'Stop live view', 'mdi:stop']]
      : [['live', 'timed', `Live view · ${mmss(this._liveSeconds())}`, 'mdi:play'], ['live', 'continuous', 'Live view · continuous', 'mdi:all-inclusive']]);
    // Record a clip (live or idle: the action opens the stream itself); disabled while one is recorded
    if (avail && canLive) liveMenu.push(['live', 'rec', recording ? 'Recording a clip' : 'Record a clip', 'mdi:record-rec', recording]);
    const actMenu = ['capture', 'refresh'].filter(a => E[a] && avail).map(a => ['action', a, ACTIONS[a].label, ACTIONS[a].icon]);
    const items = [...liveMenu, ...actMenu];
    const menu = this._menuOpen ? `
          <div class="menu" role="menu" aria-label="Camera actions">
            ${items.map(([k, v, l, i, off], n) => `<button type="button" role="menuitem" class="mi${k === 'action' && stg.act === v ? ' sel' : ''}${k === 'action' && n === liveMenu.length && n ? ' sep' : ''}" data-${k}="${v}" data-focus="mi-${n}"${off ? ' disabled' : ''}>
              <ha-icon icon="${i}"></ha-icon><span>${escapeHtml(l)}</span>${k === 'action' && stg.act === v ? '<ha-icon class="chk" icon="mdi:check"></ha-icon>' : ''}</button>`).join('')}
          </div>` : '';
    const btnLabel = `${word}${detail.length ? ` · ${detail.join(' · ')}` : ''}; actions`;
    const hdr = `
        <div class="hdr${nStaged ? ' staging' : ''}">
          <div class="tline">
            <div class="title">
              <div class="bwrap">
                <button type="button" class="mbtn st-${cls}${stg.act ? ' staged' : ''}${actSt.status === 'failed' ? ' failed' : ''}" data-menu data-focus="menu"
                  aria-haspopup="menu" aria-expanded="${!!this._menuOpen}" aria-label="${escapeHtml(btnLabel)}"><ha-icon icon="${avail ? 'mdi:cctv' : 'mdi:cctv-off'}"></ha-icon>${badge ? `<i class="badge ${badge[0]}"><ha-icon icon="${badge[1]}"></ha-icon></i>` : ''}</button>
                ${menu}
              </div>
              <div class="htext">
                <div class="name">${escapeHtml(name)}</div>
                <div class="hsub"><span class="g"><span class="st${wordCls}">${escapeHtml(shownWord)}</span></span>${detail.map(d => `<span class="g"><i class="sep">·</i><span>${escapeHtml(d)}</span></span>`).join('')}</div>
              </div>
            </div>
            <div class="acts${nStaged ? '' : ' off" inert aria-hidden="true'}">
              <button class="btn icon" type="button" data-act="cancel" data-focus="act-cancel" aria-label="Cancel the changes"><ha-icon icon="mdi:close"></ha-icon></button>
              <button class="btn primary${nStaged && this._restartWarn() ? ' warn' : ''}" type="button" data-act="set" data-focus="act-set" aria-label="Set: ${escapeHtml(nStaged ? this._hint() : '')}">Set<span class="cnt${nStaged > 1 ? '' : ' none'}" aria-hidden="true">${nStaged > 1 ? nStaged : 0}</span></button>
            </div>
          </div>
        </div>`;

    // ---- picture overlay ----
    const still = this._image('still', this._picUrl(st, true));
    const srcKind = st && st.attributes.image_source;
    const stillMs = st && st.attributes.triggered_at ? Date.parse(st.attributes.triggered_at) : NaN;
    const chip = (pos, c2, inner, label, info) => `<${info ? `button type="button" data-info="${escapeHtml(info)}"` : 'span'} class="chip ${pos}${c2 ? ` ${c2}` : ''}" aria-label="${escapeHtml(label)}">${inner}</${info ? 'button' : 'span'}>`;
    const batCls = isNaN(bat) ? '' : (bat >= 70 ? 'hi' : (bat >= 30 ? 'mid' : 'lo'));
    const chgSt = this._st(E.charging), solSt = this._st(E.solarCharging);
    const charging = chgSt ? chgSt.state === 'on' : (solSt ? solSt.state === 'on' : false);
    const solar = solSt ? solSt.state === 'on' : !!(chgSt && chgSt.state === 'on' && chgSt.attributes.solar_charging);
    const b10 = Math.max(10, Math.round(bat / 10) * 10);
    const batIcon = isNaN(bat) ? 'mdi:battery-unknown' : (charging ? (bat >= 95 ? 'mdi:battery-charging-100' : `mdi:battery-charging-${b10}`)
      : (bat >= 95 ? 'mdi:battery' : `mdi:battery-${b10}`));
    const batText = `Battery ${isNaN(bat) ? 'unknown' : `${Math.round(bat)} %`}${charging ? (solar ? ', charging from solar' : ', charging') : ''}`;
    const tr = E.battery ? chip('tr', batCls, `<ha-icon icon="${batIcon}"></ha-icon><span>${isNaN(bat) ? '--' : `${Math.round(bat)} %`}</span>${solar ? '<ha-icon class="sun" icon="mdi:solar-power-variant"></ha-icon>' : ''}`, batText, E.battery) : '';
    // A history picture shown in place of the still: its own chip with a close button, no live or pan/tilt controls
    const hItems = (this._hist && this._hist.items) || [];
    const sItems = (this._srec && this._srec.items) || [];
    const vItem = this._view && !L ? (hItems.find(x => x.id === this._view) || sItems.find(x => x.id === this._view)) : null;
    if (this._view && !vItem && ((this._hist && this._hist.items) || String(this._view).startsWith('rec:'))) this._view = null;
    const vUrl = vItem ? this._histUrl(vItem.station ? vItem.mcid : vItem.id) : null;
    const vPic = vItem ? (vItem.video ? { src: vUrl } : this._image('view', vUrl)) : null;
    const vPoster = vItem && vItem.video ? (vItem.thumb || (vItem.posterId ? this._histUrl(vItem.posterId) : null)) : null;
    let tl = '';
    if (vItem) {
      const lb = this._histLabel(vItem);
      tl = `<span class="chip tl hchip"><ha-icon icon="${lb.icon}"></ha-icon><span>${escapeHtml(`${lb.kind} · ${lb.when}`)}</span></span>
            <button type="button" class="hx" data-histclose data-focus="hist-close" aria-label="Back to the current still"><ha-icon icon="mdi:close"></ha-icon></button>`;
    // While the controls are faded only the chip's red dot shows
    } else if (L && L.video) tl = `<span class="chip tl rec live" role="img" aria-label="${L.continuous ? 'Live, continuous' : 'Live'}"><i class="dot"></i><span>LIVE</span><span class="tv" data-tick></span></span>`;
    // The integration keeps no still until a detection or New still; it never wakes the camera for one
    else if (!L && avail && still.err) tl = chip('tl', '', '<ha-icon icon="mdi:image-off-outline"></ha-icon><span>No still yet</span>', 'No still yet');
    else if (!L && avail && still.src) {
      const t = srcKind === 'live' ? 'Snapshot' : `Event${isNaN(stillMs) ? '' : ` · ${this._ago(stillMs)}`}`;
      tl = chip('tl', '', `<ha-icon icon="${srcKind === 'live' ? 'mdi:camera-iris' : 'mdi:motion-sensor'}"></ha-icon><span>${escapeHtml(t)}</span>`, `Still: ${t}`);
    }
    // Recording a clip: its own chip with the seconds so far; it stays while the live controls are faded
    const recChip = recording && !vItem ? chip('', 'rec recc', `<i class="dot"></i><span class="rw">REC</span><span class="tv" data-rect>${this._recSeconds()} s</span>`, 'Recording a clip') : '';
    const centre = vItem ? (vPic && vPic.src ? '' : '<div class="ctr wake" role="status"><i class="spin"></i></div>') : !avail
      ? '<div class="ctr off"><ha-icon icon="mdi:cctv-off"></ha-icon><span>Unavailable</span></div>'
      : (!L && !canLive ? '<div class="ctr off"><ha-icon icon="mdi:video-off-outline"></ha-icon><span>No live view for this model</span></div>'
        : !L ? `<div class="ctr">
            <button type="button" class="play" data-live="timed" data-focus="play" aria-label="Start live view for ${mmss(this._liveSeconds())}"><ha-icon icon="mdi:play"></ha-icon></button>
            <button type="button" class="pin0" data-live="continuous" data-focus="pin0" aria-label="Start live view, continuous"><ha-icon icon="mdi:all-inclusive"></ha-icon></button>
            <span class="plbl" aria-hidden="true">Live · ${mmss(this._liveSeconds())}</span>
          </div>`
        : (!L.video ? '<div class="ctr wake" role="status"><i class="spin"></i><span class="wt">Waking camera…</span><span class="ws" data-wake></span></div>' : ''));
    const aimOK = avail && !!L && L.video;
    const canPre = aimOK && E.hasPan && presetIdx.length > 0;
    if (!canPre) this._preOpen = false;
    const bar = L ? `<div class="lbar" role="group" aria-label="Live view">
            <button type="button" class="lb" data-live="stop" data-focus="live-stop" aria-label="Stop live view"><ha-icon icon="mdi:stop"></ha-icon></button>
            <button type="button" class="lb opt2${L.continuous ? ' on' : ''}" data-live="pin" data-focus="live-pin" aria-pressed="${L.continuous}" aria-label="Continuous live view"><ha-icon icon="mdi:all-inclusive"></ha-icon></button>
            <button type="button" class="lb rec${recording ? ' on' : ''}" data-live="rec" data-focus="live-rec" aria-label="${recording ? 'Recording a clip' : 'Record a clip'}"${recording ? ' disabled' : ''}><ha-icon icon="mdi:record-rec"></ha-icon></button>
            ${L.video ? `<button type="button" class="lb" data-live="mute" data-focus="live-mute" aria-pressed="${!L.muted}" aria-label="${L.muted ? 'Unmute' : 'Mute'}"><ha-icon icon="${L.muted ? 'mdi:volume-off' : 'mdi:volume-high'}"></ha-icon></button>` : ''}
            ${canPre ? `<button type="button" class="lb${this._preOpen ? ' on' : ''}" data-live="presets" data-focus="live-presets" aria-expanded="${!!this._preOpen}" aria-label="Presets"><ha-icon icon="mdi:crosshairs-gps"></ha-icon></button>` : ''}
            <button type="button" class="lb opt2" data-live="full" data-focus="live-full" aria-label="${this._isFull() ? 'Exit full screen' : 'Full screen'}"><ha-icon icon="${this._isFull() ? 'mdi:fullscreen-exit' : 'mdi:fullscreen'}"></ha-icon></button>
          </div>` : '';
    // Presets over the live picture, above the bar: the camera turns there at once (the move lane)
    const lpre = canPre && this._preOpen ? `<div class="lpre" role="group" aria-label="Go to preset">${presetIdx.map((i) => {
      const imSt = this._st(E.presetImages[i]);
      const src = this._image(`preset-${i}`, ok(imSt) ? this._tileUrl(imSt) : null).src;
      const m = this._moveState(`goto-${i}`);
      const at = this._atPreset === i && !this._moves.length;
      return `<button type="button" class="lp${src ? ' pic' : ''}${m.run ? ' busy' : ''}${at ? ' at' : ''}" data-goto="${i}" data-focus="lp-${i}" aria-pressed="${at}" aria-label="Go to preset ${i + 1}${i === defIdx ? ' (default)' : ''}">
              ${src ? `<img src="${escapeHtml(src)}" alt="" draggable="false">` : '<ha-icon icon="mdi:crosshairs-gps"></ha-icon>'}
              <span class="pn">P${i + 1}${i === defIdx ? ' <ha-icon icon="mdi:home-outline"></ha-icon>' : ''}</span>
            </button>`;
    }).join('')}</div>` : '';
    const dis = !avail;
    // Pan/tilt and go-to only while the live picture shows: without it a move has no visible effect (the still
    // stays) and the camera turns back to its default preset once idle. Zoom stays: it is kept for the next view.
    const aim = avail && !!L && L.video;
    const noAim = aim ? '' : ' (start live view first)';
    const mv = (key) => {
      const s = this._moveState(key);
      return { cls: s.run ? ' busy' : '', q: s.n ? `<i class="qn" aria-hidden="true">${s.n}</i>` : '', lbl: s.n ? `, ${s.n} more waiting` : '' };
    };
    const pbtn = (d, icon, label) => { const m = mv(`pt-${d}`); return `<button type="button" class="pb ${d}${m.cls}" data-pt="${d}" data-focus="pt-${d}" aria-label="${label}${m.lbl}${noAim}"${!aim || !E[{ left: 'panLeft', right: 'panRight', up: 'tiltUp', down: 'tiltDown' }[d]] ? ' disabled' : ''}><ha-icon icon="${icon}"></ha-icon>${m.q}</button>`; };
    const pad = E.hasPan ? `<div class="ptz${aim ? '' : ' off'}" role="group" aria-label="Pan and tilt${noAim}">
            ${pbtn('up', 'mdi:chevron-up', 'Tilt up')}${pbtn('left', 'mdi:chevron-left', 'Pan left')}${pbtn('right', 'mdi:chevron-right', 'Pan right')}${pbtn('down', 'mdi:chevron-down', 'Tilt down')}
            ${!isNaN(defIdx) ? `<button type="button" class="pb hbtn${mv(`goto-${defIdx}`).cls}" data-goto="${defIdx}" data-focus="pt-home" aria-label="Go to the default preset${noAim}"${!aim ? ' disabled' : ''}><ha-icon icon="mdi:home-outline"></ha-icon></button>` : '<i class="hub"></i>'}
          </div>` : '';
    // Zoom: magnifier + / − in whole steps, the value between them, and "↺ 1×" under them while zoomed. The value
    // shows the target at once; the camera follows within ~1 s. Works without live view (kept for the next one).
    const zShown = this._zoomTarget !== undefined ? this._zoomTarget : zoom;
    const zBusy = Object.keys(this._busy).some(k => k.startsWith('zoom'));
    const zpill = E.zoom ? `<div class="zp${zShown > zMin ? ' zoomed' : ''}" role="group" aria-label="Zoom ${fmtZoom(zShown)}">
            <button type="button" class="zb${this._busy['zoom-in'] ? ' busy' : ''}" data-zoom="in" data-focus="zoom-in" aria-label="Zoom in"${dis || zShown >= zMax ? ' disabled' : ''}><ha-icon icon="mdi:magnify-plus-outline"></ha-icon></button>
            <span class="zv${zBusy ? ' pend' : ''}" aria-live="polite">${escapeHtml(fmtZoom(zShown))}</span>
            <button type="button" class="zb${this._busy['zoom-out'] ? ' busy' : ''}" data-zoom="out" data-focus="zoom-out" aria-label="Zoom out"${dis || zShown <= zMin ? ' disabled' : ''}><ha-icon icon="mdi:magnify-minus-outline"></ha-icon></button>
            ${zShown > zMin ? `<button type="button" class="zr${this._busy['zoom-reset'] ? ' busy' : ''}" data-zoom="reset" data-focus="zoom-reset" aria-label="Reset zoom to ${fmtZoom(zMin)}"${dis ? ' disabled' : ''}><ha-icon icon="mdi:restore"></ha-icon><span>${escapeHtml(fmtZoom(zMin))}</span></button>` : ''}
          </div>` : '';
    // The message takes the chip line for TOAST_MS: one line, the chips step aside
    const toast = this._toastText ? `<div class="toast" role="status" aria-label="${escapeHtml(this._toastText)}"><ha-icon icon="mdi:alert-circle-outline"></ha-icon><span>${escapeHtml(this._toastText)}</span></div>` : '';
    const over = `
        <div class="still${L && L.video ? ' gone' : ''}${L && !L.video ? ' dim' : ''}">${still.src ? `<img class="stillimg" src="${escapeHtml(still.src)}" alt="" draggable="false">` : ''}</div>
        ${vItem ? `<div class="still hview">${vPic && vPic.src ? (vItem.video
    ? `<video class="stillimg hvid" src="${escapeHtml(vPic.src)}"${vPoster ? ` poster="${escapeHtml(vPoster)}"` : ''} controls autoplay muted playsinline preload="metadata" aria-label="${escapeHtml(`${this._histLabel(vItem).kind} video, ${this._histLabel(vItem).when}`)}"></video>`
    : `<img class="stillimg" src="${escapeHtml(vPic.src)}" alt="" draggable="false">`) : ''}</div>` : ''}
        <div class="ovl${L ? ' live' : ''}${aimOK ? ' aim' : ''}${vItem && vItem.video ? ' hvo' : ''}">
          ${toast ? '' : (vItem ? tl : `<div class="crow">${tl}${recChip}${tr}</div>`)}${centre}${vItem ? '' : bar + lpre}
          ${!vItem && (pad || zpill) ? `<div class="ptzw">${zpill}${pad}</div>` : ''}
          ${toast}
        </div>`;

    // ---- section tabs and sections ----
    const setOpen = this._open === 'settings' && rows.length > 0;
    const histOpen = this._open === 'history';
    // A tab marks staged changes (dot) and failed writes (red) of rows it hides
    const tabOf = r => (r.dep ? (rows.find(x => x.id === r.dep) || r).group.id : r.group.id);
    const tabMark = id => (rows.some(r => tabOf(r) === id && r.st.status === 'failed') ? 'err'
      : (rows.some(r => tabOf(r) === id && (r.staged || r.st.status === 'pending')) ? 'stg' : ''));
    // A section tab (the first tab level); a closed Settings carries the dot (staged or sending) or red
    const secBtn = ([id, label, icon], open, m) => `<button class="sec${open ? ' on' : ''}${m ? ` ${m}` : ''}" type="button" data-more="${id}" data-focus="more-${id}" aria-expanded="${open}"
        aria-label="${escapeHtml(`${label}${m === 'err' ? ', not applied' : (m === 'stg' ? ', changed' : '')}`)}"><ha-icon icon="${icon}"></ha-icon><span>${label}</span><i class="tdot" aria-hidden="true"></i></button>`;
    const statusWord = s => (s.status === 'pending' ? 'sending' : (s.status === 'failed' ? 'Not applied' : (s.status === 'unsure' ? 'may still apply' : '')));
    const slider = (r, label) => {
      const c = r.range;
      const frac = v => Math.min(1, Math.max(0, (v - c.min) / ((c.max - c.min) || 1)));
      const was = r.staged && !isNaN(c.value) ? frac(c.value) : NaN;
      return `${!isNaN(was) ? `<button class="ghost" type="button" data-restore="${escapeHtml(r.id)}" style="--at:${was.toFixed(4)}" aria-label="Put ${escapeHtml(label)} back to ${escapeHtml(c.fmt(c.value))}"><i></i></button>` : ''}
              <input class="ctl-range${isNaN(r.shown) ? ' unk' : ''}" type="range" data-kind="${escapeHtml(r.id)}" data-focus="range-${escapeHtml(r.id)}" min="${c.min}" max="${c.max}" step="${c.step}" value="${isNaN(r.shown) ? c.min : r.shown}"
                aria-label="${escapeHtml(label)}" aria-valuetext="${escapeHtml(r.value)}" style="--pos:${(frac(isNaN(r.shown) ? c.min : r.shown) * 100).toFixed(1)}%"${r.off ? ' disabled' : ''}>`;
    };
    // A dropdown: the value on a button; the list opens inside the card (see _placeDd)
    const ddHtml = (r) => {
      const d = r.dd;
      return `<button class="dd" type="button" data-dd="${escapeHtml(r.id)}" data-focus="dd-${escapeHtml(r.id)}" aria-haspopup="listbox" aria-expanded="${d.open}" aria-label="${escapeHtml(`${r.label}: ${d.shown}`)}"${r.off ? ' disabled' : ''}><span>${escapeHtml(d.shown)}</span><ha-icon icon="${d.open ? 'mdi:menu-up' : 'mdi:menu-down'}"></ha-icon></button>
              ${d.open ? `<div class="ddl" role="listbox" aria-label="${escapeHtml(r.label)}">${d.opts.map(o => `<button class="ddo${o.live ? ' live' : ''}" type="button" role="option" data-kind="${escapeHtml(r.id)}" data-opt="${escapeHtml(o.v)}" data-focus="ddo-${escapeHtml(r.id)}-${escapeHtml(o.v)}" aria-selected="${o.on}"><span>${escapeHtml(o.l)}</span>${o.on ? '<ha-icon class="chk" icon="mdi:check"></ha-icon>' : ''}</button>`).join('')}</div>` : ''}`;
    };
    const tgHtml = r => `<button class="tg${r.tg.on ? ' on' : ''}${r.tg.known ? '' : ' unk'}" type="button" role="switch" data-tg="${escapeHtml(r.id)}" data-focus="tg-${escapeHtml(r.id)}" aria-checked="${r.tg.on}" aria-label="${escapeHtml(r.label)}"${r.off ? ' disabled' : ''}><i></i></button>`;
    // A segment row with no option selected keeps its value text ("--", "Not reported")
    const shownUnknown = r => !!r.seg && !r.seg.some(o => o.on);
    const rowHtml = (r, first) => {
      const w = statusWord(r.st);
      if (r.tg) {
        return `
          <div class="row tgrow${first ? ' tg1' : ''}${r.staged ? ' staged' : ''}${r.st.status ? ` ${r.st.status}` : ''}${r.off ? ' na' : ''}${r.dep ? ' dep' : ''}">
            <div class="rname"><ha-icon icon="${escapeHtml(r.icon)}"></ha-icon><span class="rn">${escapeHtml(r.label)}</span>${w || r.sub ? `<span class="rsub${r.st.status === 'failed' ? ' err' : ''}">${escapeHtml(w || r.sub)}</span>` : ''}</div>
            ${tgHtml(r)}
          </div>`;
      }
      const ctl = r.range ? slider(r, r.label) : (r.dd ? ddHtml(r)
        : `<div class="seg${r.segLong ? ' long' : ''}" role="group" aria-label="${escapeHtml(r.label)}">${r.seg.map(o => `<button class="opt${o.i ? '' : ' txt'}" type="button" data-kind="${escapeHtml(r.id)}" data-opt="${escapeHtml(o.v)}" data-focus="opt-${escapeHtml(r.id)}-${escapeHtml(o.v)}" aria-pressed="${o.on}" aria-label="${escapeHtml(o.full || o.l)}"${r.off ? ' disabled' : ''}>${o.i ? `<ha-icon icon="${o.i}"></ha-icon>` : ''}<span>${escapeHtml(o.l)}</span></button>`).join('')}</div>`);
      return `
          <div class="row${r.staged ? ' staged' : ''}${r.st.status ? ` ${r.st.status}` : ''}${r.off ? ' na' : ''}${r.dd ? ' ddrow' : ''}${r.dd && r.dd.open ? ' ddopen' : ''}${r.dep ? ' dep' : ''}${r.seg ? ` segrow${r.segIcons ? ' segicons' : ''}` : ''}${r.seg && shownUnknown(r) ? ' segunk' : ''}">
            <div class="rname"><ha-icon icon="${escapeHtml(r.icon)}"></ha-icon><span class="rn">${escapeHtml(r.label)}</span>${w || r.sub ? `<span class="rsub${r.st.status === 'failed' ? ' err' : (r.warn && !w ? ' warn' : '')}">${escapeHtml(w || r.sub)}</span>` : ''}</div>
            <span class="rval${r.unrep ? ' unrep' : ''}">${escapeHtml(r.value)}</span>
            <div class="rctl">${ctl}</div>
          </div>`;
    };
    // Power readings (charging, solar, battery temperature, counters): one line of chips, each opens its history
    const statHtml = () => {
      const chips = [];
      const chg = this._st(E.charging), sol = this._st(E.solarCharging);
      const solarOn = sol ? sol.state === 'on' : !!(chg && chg.attributes.solar_charging);
      if (chg || sol) {
        const t = !ok(chg || sol) ? '--' : ((chg ? chg.state === 'on' : solarOn) ? (solarOn ? 'Solar charging' : 'Charging') : 'Not charging');
        chips.push([E.solarCharging || E.charging, solarOn ? 'mdi:solar-power-variant' : (t === 'Charging' ? 'mdi:battery-charging' : 'mdi:power-plug-off-outline'), t, t]);
      }
      POWER_STATS.forEach(([k, lbl, icon]) => {
        const sx = this._st(E[k]);
        if (!sx) return;
        const u = sx.attributes.unit_of_measurement;
        const v = ok(sx) ? `${sx.state}${u ? (u === '°C' || u === '°F' ? u : ` ${u}`) : ''}` : '--';
        const name = sx.attributes.friendly_name ? this._rowLabel(E[k], k, sx) : lbl;
        chips.push([E[k], icon, lbl ? (k === 'solarIntensity' ? `${lbl} ${v}` : `${v} ${lbl}`) : v, `${name}: ${v}`]);
      });
      // Events over the same period: recorded of detected, one chip (opens the recorded count)
      const det = this._st(E.detected), rec = this._st(E.recorded);
      if (det || rec) {
        const n = x => (ok(x) ? x.state : '--');
        chips.push([E.recorded || E.detected, 'mdi:record-rec', `${n(rec)} of ${n(det)} recorded`, `Recorded events ${n(rec)} of ${n(det)} detected`]);
      }
      if (!chips.length) return '';
      return `<div class="pstats">${chips.map(([id, i, t, a]) => `<button type="button" class="pst" data-info="${escapeHtml(id)}" aria-label="${escapeHtml(a)}"><ha-icon icon="${i}"></ha-icon><span>${escapeHtml(t)}</span></button>`).join('')}</div>`;
    };
    // Rows under their group's heading; a controller's dependents in a block under it; More settings behind a toggle
    const tabHtml = (id) => {
      const g = groupInfo(id);
      const on = this._tab === id, m = tabMark(id);
      const n = id === 'more' ? nMore : 0;
      return `<button class="stab${on ? ' on' : ''}${m ? ` ${m}` : ''}" type="button" role="tab" data-stab="${id}" data-focus="stab-${id}" aria-selected="${on}"
        tabindex="${on ? 0 : -1}" aria-label="${escapeHtml(`${g[1]}${n ? `, ${n} settings` : ''}${m === 'err' ? ', not applied' : (m === 'stg' ? ', changed' : '')}`)}"><ha-icon icon="${g[2]}"></ha-icon><span>${g[1]}</span><i class="tdot" aria-hidden="true"></i></button>`;
    };
    const rowsHtml = () => {
      const tn = this._tab === 'more' ? 'More settings' : (GROUPS.find(x => x[0] === this._tab) || [])[1];
      let out = `<div class="stabs" role="tablist" aria-label="Settings">${tabs.map(tabHtml).join('')}<div class="stitle" aria-hidden="true">${escapeHtml(tn || '')}</div></div>`;
      if (this._tab === 'power') out += statHtml();
      let prevTg = true;
      rows.forEach((r) => {
        if (r.dep || r.group.id !== this._tab) return;
        if (!r.deps) { const h = rowHtml(r, !!r.tg && !prevTg); prevTg = !!r.tg; out += h; return; }
        prevTg = false;
        const cap = r.depOpt ? `Only in ${r.depOpt}` : `Depend on ${r.label}`;
        out += `<div class="cgrp">${rowHtml(r)}<div class="deps${r.depOn ? ' on' : ''}" role="group" aria-label="${escapeHtml(`${cap}${r.depOn ? '' : ', not in use now'}`)}"><div class="dcap" aria-hidden="true">${escapeHtml(cap)}</div>${r.deps.map(rowHtml).join('')}</div></div>`;
      });
      return out;
    };
    const histTile = (it) => {
      const lb = this._histLabel(it);
      // A video tile shows its thumbnail, else the picture saved at the same second, else a film icon
      const u = it.video ? (it.thumb || (it.posterId ? this._histUrl(it.posterId) : null)) : this._histUrl(it.id);
      const on = this._view === it.id;
      return `<button type="button" class="ht${on ? ' on' : ''}${it.video ? ' vid' : ''}" data-hist="${escapeHtml(it.id)}" data-focus="hist-${escapeHtml(it.id)}" aria-pressed="${on}" aria-label="${escapeHtml(`${lb.kind}${it.video ? ' video' : ''}, ${lb.when}`)}">
            ${u ? `<img src="${escapeHtml(u)}" alt="" loading="lazy" draggable="false">` : (it.video ? '<ha-icon class="hph" icon="mdi:filmstrip"></ha-icon>' : '')}
            ${it.video ? '<span class="hvb" aria-hidden="true"><ha-icon icon="mdi:play"></ha-icon></span>' : ''}
            <span class="hl"><ha-icon icon="${lb.icon}"></ha-icon><span>${escapeHtml(lb.when)}</span></span>
          </button>`;
    };
    // Sub-tabs: Events always (it is the one opened first), Captures always, Presets only when the folder has any,
    // Station unless the integration answered that the camera has no station recordings (decided when History opens,
    // so the tabs never move while it is open)
    if (this._stationTab === undefined) this._stationTab = STATION_SUPPORT.get(this._config.entity) !== false;
    const hTabs = HIST_TABS.filter(t => (t[0] !== 'presets' || hItems.some(x => histTab(x.kind) === 'presets')) && (t[0] !== 'station' || this._stationTab));
    if (!hTabs.some(t => t[0] === this._htab)) this._htab = 'events';
    const hf = { pic: true, vid: true, ...(this._hfilt[this._htab] || {}) };
    const hDay = this._hday;
    const inTab = hItems.filter(x => histTab(x.kind) === this._htab && (!hDay || x.date === hDay));
    const nVid = inTab.filter(x => x.video).length, nPic = inTab.length - nVid;
    // The first _hn of the filtered list; Show more at the strip's end adds the next HISTORY_PAGE (only shown tiles
    // resolve their files)
    const hList = inTab.filter(x => (x.video ? hf.vid : hf.pic));
    const hShown = hList.slice(0, this._hn);
    const hRest = hList.length - hShown.length;
    const hMore = hRest > 0 ? `<button type="button" class="hmore" data-hmore data-focus="hmore"
            aria-label="${escapeHtml(`Show ${Math.min(HISTORY_PAGE, hRest)} more, ${hShown.length} of ${hList.length} shown`)}"><ha-icon icon="mdi:chevron-double-right"></ha-icon><span>Show more</span></button>` : '';
    const hTab = HIST_TABS.find(t => t[0] === this._htab);
    const htabHtml = (t) => {
      const on = this._htab === t[0];
      return `<button class="stab${on ? ' on' : ''}" type="button" role="tab" data-htab="${t[0]}" data-focus="htab-${t[0]}" aria-selected="${on}" tabindex="${on ? 0 : -1}"
        aria-label="${escapeHtml(t[1])}"><ha-icon icon="${t[2]}"></ha-icon><span>${t[1]}</span></button>`;
    };
    const hfBtn = (k, icon, label, n) => {
      const last = hf[k] && !hf[k === 'pic' ? 'vid' : 'pic'];
      return `<button class="hf${hf[k] ? ' on' : ''}" type="button" data-hf="${k}" data-focus="hf-${k}" aria-pressed="${hf[k]}"${last ? ' aria-disabled="true"' : ''}
        aria-label="${escapeHtml(`${label}, ${n} in ${hTab[1]}${last ? ', only kind shown' : ''}`)}"><ha-icon icon="${icon}"></ha-icon><span>${n}</span></button>`;
    };
    // Go to a day: the calendar alone at rest; the chosen day (its text hidden on narrow cards) and a button back to
    // the newest once set
    const dayText = (d) => { const [y, mo, dd] = d.split('-'); return `${dd}.${mo}${y === this._today().slice(0, 4) ? '' : `.${y}`}`; };
    const dayBtns = hDay
      ? `<button class="hf hday on" type="button" data-hday data-focus="hday" aria-label="${escapeHtml(`Showing ${dayText(hDay)}, choose another day`)}"><ha-icon icon="mdi:calendar"></ha-icon><span>${dayText(hDay)}</span></button><button class="hf hdx" type="button" data-hdayclear data-focus="hdayclear" aria-label="Back to the newest"><ha-icon icon="mdi:close"></ha-icon></button>`
      : '<button class="hf hday" type="button" data-hday data-focus="hday" aria-label="Go to a day"><ha-icon icon="mdi:calendar"></ha-icon></button>';
    const S = this._srec;
    const sOpen = this._htab === 'station';
    const sCount = S && S.items ? String(S.items.length) : '–';
    // Station: the recordings' count and a refresh button where the media filter sits on the other sub-tabs, then the
    // day button
    const sTools = `<div class="hfilt" role="group" aria-label="Station recordings">
            <span class="hf stc" role="status" aria-label="${escapeHtml(`${S && S.items ? S.items.length : 'No'} recordings listed${S && S.more ? ', more on the station' : ''}`)}"><ha-icon icon="mdi:filmstrip"></ha-icon><span>${sCount}</span></span>
            <button class="hf srf${S && S.loading ? ' ld' : ''}" type="button" data-srefresh data-focus="srefresh" aria-label="Refresh the station list${S && S.loading ? ', loading' : ''}"><ha-icon icon="mdi:refresh"></ha-icon></button>${dayBtns}</div>`;
    const hBar = `<div class="hbar"><div class="stabs htabs" role="tablist" aria-label="History">${hTabs.map(htabHtml).join('')}</div>
          ${sOpen ? sTools : `<div class="hfilt" role="group" aria-label="Show in ${escapeHtml(hTab[1])}">${hfBtn('pic', 'mdi:image-outline', 'Pictures', nPic)}${hfBtn('vid', 'mdi:filmstrip', 'Videos', nVid)}${dayBtns}</div>`}</div>`;
    // A station recording: thumbnail, start, length, kind and whether it is saved or still being written. Play and
    // Save both fetch it from the station into the history folder (5-10 s); Play then opens it in the picture area.
    const recRow = (it) => {
      const lb = this._histLabel(it);
      const f = this._sfetch.get(it.rid);
      const on = this._view === it.id;
      const dur = it.dur !== null && isFinite(it.dur) ? mmss(Math.round(it.dur)) : '';
      const state = it.saved ? 'Saved' : (it.settled ? '' : 'Recording');
      const th = it.thumb && !THUMB_BAD.has(it.thumb) ? it.thumb : null;
      const img = th ? (this._thumbs.has(th) ? `<img src="${escapeHtml(th)}" alt="" draggable="false">` : `<img data-src="${escapeHtml(th)}" alt="" draggable="false">`) : '<ha-icon icon="mdi:filmstrip"></ha-icon>';
      const what = `${lb.kind}, ${lb.when}${dur ? `, ${dur}` : ''}`;
      const btn = (k, icon, label, off) => `<button class="sb${f === k ? ' run' : ''}" type="button" data-r${k}="${it.rid}" data-focus="${k === 'play' ? `hist-${escapeHtml(it.id)}` : `rsave-${it.rid}`}"
              aria-label="${escapeHtml(`${label} ${what}${f === k ? ', fetching from the station' : ''}`)}"${f === k || (f && !off) ? ' aria-disabled="true"' : ''}${off ? ' disabled' : ''}>${f === k ? '<i class="spin sm" aria-hidden="true"></i>' : `<ha-icon icon="${icon}"></ha-icon>`}</button>`;
      return `<div class="srow${on ? ' on' : ''}${it.settled ? '' : ' unset'}" role="listitem">
            <span class="sth">${img}</span>
            <span class="stx"><span class="sl1"><span class="swhen">${escapeHtml(lb.when)}</span>${dur ? `<span class="sdur">${dur}</span>` : ''}</span>
              <span class="sl2"><span class="skind"><ha-icon icon="${lb.icon}"></ha-icon><span>${escapeHtml(lb.kind)}</span></span>${state ? `<span class="sst${it.saved ? ' ok' : ''}">${state}</span>` : ''}</span></span>
            ${btn('play', 'mdi:play', 'Play', !it.settled)}${btn('save', it.saved ? 'mdi:check' : 'mdi:download', it.saved ? 'Saved:' : 'Save to the history:', !it.settled || it.saved)}
          </div>`;
    };
    const sMsg = t => `<div class="slist msg" role="status"><div class="hmsg">${t}</div></div>`;
    // The next page of the station's list, as the list's last row (inside its scroll area)
    const sMore = S && S.more && !S.loading ? `<div class="srow smore" role="listitem"><button class="smb" type="button" data-smore data-focus="smore"
            aria-label="${escapeHtml(`Show ${HISTORY_PAGE} more recordings${S.moreLoading ? ', loading from the station' : ''}`)}"${S.moreLoading ? ' aria-disabled="true"' : ''}>${S.moreLoading ? '<i class="spin sm" aria-hidden="true"></i>' : '<ha-icon icon="mdi:chevron-double-down"></ha-icon>'}<span>Show more</span></button></div>` : '';
    const stationBody = !S || (!S.items && S.loading) ? sMsg('<i class="spin sm" aria-hidden="true"></i><span>Loading from the station…</span>')
      : (!S.items ? sMsg(`<ha-icon icon="mdi:alert-circle-outline"></ha-icon><span>${escapeHtml(S.err || 'Not loaded')}</span>`)
        : (!S.supported ? sMsg('<span>This camera keeps no recordings on a station</span>')
          : (!S.items.length ? sMsg(`<span>${hDay ? `No recordings on ${dayText(hDay)}` : HIST_TABS[3][3]}</span>`)
            : `<div class="slist" role="list" aria-label="Station recordings"${S.loading ? ' aria-busy="true"' : ''}>${S.items.map(recRow).join('')}${sMore}</div>`)));
    const hEmpty = !inTab.length ? (hDay ? `Nothing on ${dayText(hDay)}` : hTab[3]) : (hf.pic ? 'No pictures here' : 'No videos here');
    const histBody = sOpen ? `${hBar}${stationBody}` : !this._hist || !this._hist.items
      ? `${hBar}<div class="hmsg">Loading…</div>`
      : `${hBar}${hShown.length ? `<div class="hstrip${/[^\d\s.:]/.test(clockTime(this._hass, new Date(2000, 0, 1, 22, 5))) ? ' wide' : ''}" data-tab="${this._htab}-${hf.pic ? 'p' : ''}${hf.vid ? 'v' : ''}">${hShown.map(histTile).join('')}${hMore}</div>` : `<div class="hmsg">${escapeHtml(hEmpty)}</div>`}`;
    const setMark = setOpen ? '' : (rows.some(r => r.st.status === 'failed') ? 'err' : (rows.some(r => r.staged || r.st.status === 'pending') ? 'stg' : ''));
    const lower = `
        <div class="secs" role="group" aria-label="Sections">
          ${secBtn(SECTIONS[0], histOpen, '')}
          ${rows.length ? secBtn(SECTIONS[1], setOpen, setMark) : ''}
        </div>
        ${histOpen ? `<div class="hist${/[^\d\s.:]/.test(clockTime(this._hass, new Date(2000, 0, 1, 22, 5))) ? ' wide' : ''}">${histBody}</div>` : ''}
        ${setOpen ? `<div class="ctl-rows">${rowsHtml()}</div>` : ''}`;

    const compact = this._config.layout === 'compact' || (this._config.layout !== 'regular' && this._width !== undefined && this._width < COMPACT_W);
    this.content.classList.toggle('compact', compact);
    this._pic.classList.toggle('live', !!L);
    this._pic.classList.toggle('has-ptz', !!(E.hasPan || E.zoom));
    const active0 = this.shadowRoot.activeElement;
    const fk = active0 && active0.dataset ? active0.dataset.focus : null;
    // The station list keeps its scroll position when it is drawn again (a row's spinner, a refresh)
    const sl0 = this._lowerEl.querySelector('.slist');
    const sTop = sl0 ? sl0.scrollTop : 0;
    // ... and the tile strip its sideways position within one sub-tab (Show more, a resolved picture)
    const hs0 = this._lowerEl.querySelector('.hstrip');
    const hLeft = hs0 ? [hs0.dataset.tab, hs0.scrollLeft] : null;
    let changed = false;
    [['hdr', this._hdrEl, hdr], ['over', this._overEl, over], ['lower', this._lowerEl, lower]].forEach(([k, el, html]) => {
      if (this._keys[k] === html) return;
      el.innerHTML = html;
      this._keys[k] = html;
      changed = true;
    });
    if (changed && fk) this._focus(fk);
    if (changed && sTop) { const sl = this._lowerEl.querySelector('.slist'); if (sl) sl.scrollTop = sTop; }
    if (changed && hLeft && hLeft[1]) { const hs = this._lowerEl.querySelector('.hstrip'); if (hs && hs.dataset.tab === hLeft[0]) hs.scrollLeft = hLeft[1]; }
    this._lazyThumbs();
    this._placeDd();
    this._tick();
    if (Object.keys(this._pending).length) this._arm();
  }

  _build() {
    const card = document.createElement('ha-card');
    this.content = document.createElement('div');
    // not `card-content`: ha-card pads that class, and the picture runs edge to edge
    this.content.className = 'cc';
    this.content.innerHTML = '<div class="hwrap"></div><div class="pic"><div class="vslot"></div><div class="over"></div></div><div class="lower"></div>';
    const style = document.createElement('style');
    style.textContent = CSS_TEXT;
    card.append(style, this.content);
    this.shadowRoot.appendChild(card);
    this._hdrEl = this.content.querySelector('.hwrap');
    this._pic = this.content.querySelector('.pic');
    this._vslot = this.content.querySelector('.vslot');
    this._overEl = this.content.querySelector('.over');
    this._lowerEl = this.content.querySelector('.lower');
    this.content.addEventListener('click', ev => this._onClick(ev));
    this.content.addEventListener('keydown', ev => this._onKey(ev));
    // In full screen the wheel zooms (the page cannot scroll there anyway); in the card it scrolls the page
    this.content.addEventListener('wheel', (ev) => {
      if (!this._isFull() || !this._live || !this._entities().zoom) return;
      ev.preventDefault();
      if (Math.abs(ev.deltaY) < 4) return;
      this._zoom(ev.deltaY < 0 ? 'in' : 'out');
    }, { passive: false });
    // The full-screen button's icon and label follow the state
    document.addEventListener('fullscreenchange', () => {
      if (!this.isConnected) return;
      if (this._stream) this._stream.fitMode = this._fit();
      this.render();
    });
    this._vslot.addEventListener('streams', ev => this._onStreams(ev));
    // A station thumbnail that does not load (no still on the station) becomes the film icon
    this._lowerEl.addEventListener('error', (ev) => {
      const t = ev.target;
      if (t && t.tagName === 'IMG' && t.closest('.sth')) { THUMB_BAD.add(t.getAttribute('src')); this._keys.lower = null; this.render(); }
    }, true);
    // A touch, click or key in the picture shows the live controls again; a tap on the bare picture hides them.
    // The tap that brings them back presses nothing: the control under it was invisible when the finger came down.
    this._pic.addEventListener('pointerdown', () => { this._wasQuiet = this._isQuiet(); this._awake(true); });
    // Keyboard focus only: a tapped button keeps focus on a phone and would hold the controls on screen
    this._pic.addEventListener('focusin', (ev) => { const t = ev.composedPath()[0]; if (t && t.matches && t.matches(':focus-visible')) this._awake(true); });
    // A mouse moving over the picture keeps shown controls up (the timer restarts); faded ones need a click
    this._pic.addEventListener('pointermove', (ev) => {
      if (ev.pointerType !== 'mouse' || !this._live || !this._live.video || Date.now() - (this._movedAt || 0) < 250) return;
      this._movedAt = Date.now();
      if (!this._isQuiet()) this._awake(true);
    });
    this._pic.addEventListener('click', (ev) => {
      const ctl = ev.composedPath().some(n => n.tagName && (/^(BUTTON|INPUT|VIDEO)$/.test(n.tagName)
        || (n.classList && ['lbar', 'ptzw', 'lpre', 'crow', 'toast'].some(c => n.classList.contains(c)))));
      const was = this._wasQuiet;
      this._wasQuiet = false;
      if (was && ctl && !ev.composedPath().some(n => n.classList && n.classList.contains('crow'))) { ev.stopPropagation(); ev.preventDefault(); return; }
      if (!ctl && !was) this._hush();
    }, true);
    const rangeOf = ev => ev.composedPath().find(n => n.classList && n.classList.contains('ctl-range'));
    this.content.addEventListener('input', (ev) => { const r = rangeOf(ev); if (r) this._stageRange(r); });
    this.content.addEventListener('change', (ev) => { const r = rangeOf(ev); if (r) { this._stageRange(r); this.render(); } });
    if (window.ResizeObserver) {
      this._ro = new ResizeObserver((entries) => {
        const w = Math.round(entries[entries.length - 1].contentRect.width);
        const was = this._width !== undefined && this._width < COMPACT_W;
        this._width = w;
        if (was !== (w < COMPACT_W)) this.render();
      });
      this._ro.observe(this.content);
    }
  }
}

const CSS_TEXT = `
  /* Colours from the HA theme (light and dark); the semantic --ha-color-* tokens fall back to mixes of the base ones
     on older frontends. The controls over the picture stay white on dark glass, legible on any image. */
  :host {
    --cc-bg: var(--ha-card-background, var(--card-background-color, #fff));
    --cc-text: var(--primary-text-color, #212121);
    --cc-dim: var(--secondary-text-color, #727272);
    /* secondary text on tinted surfaces: HA's secondary colour alone misses 4.5:1 there in the light theme */
    --cc-sub: color-mix(in srgb, var(--cc-dim) 70%, var(--cc-text));
    --cc-accent: var(--primary-color, #03a9f4);
    --cc-accent-text: color-mix(in srgb, var(--cc-accent) 45%, var(--cc-text));
    --cc-error: var(--error-color, #db4437);
    --cc-error-text: color-mix(in srgb, var(--cc-error) 70%, var(--cc-text));
    --cc-idle: var(--state-inactive-color, #8a8a8a);
    --cc-warn: var(--warning-color, #ffa600);
    --cc-live: var(--red-color, #f44336);
    /* text and icons on an accent or red fill */
    --cc-on-accent: var(--text-primary-color, #fff);
    --cc-sun: var(--amber-color, #ffc107);
    --cc-bat-hi: var(--state-sensor-battery-high-color, var(--success-color, #43a047));
    --cc-bat-mid: var(--state-sensor-battery-medium-color, var(--warning-color, #ffa600));
    --cc-bat-lo: var(--state-sensor-battery-low-color, var(--error-color, #db4437));
    /* control surfaces as HA's own: tracks, selected segments, form fields, switches, dividers, shadows */
    --cc-track: var(--ha-color-fill-neutral-quiet-resting, color-mix(in srgb, var(--cc-text) 6%, transparent));
    --cc-sel: var(--ha-color-fill-primary-normal-resting, color-mix(in srgb, var(--cc-accent) 20%, var(--cc-bg)));
    --cc-sel-quiet: var(--ha-color-fill-primary-quiet-resting, color-mix(in srgb, var(--cc-accent) 12%, transparent));
    --cc-field: var(--ha-color-form-background, color-mix(in srgb, var(--cc-text) 5%, transparent));
    --cc-field-line: var(--ha-color-border-neutral-quiet, color-mix(in srgb, var(--cc-text) 16%, transparent));
    --cc-sw-off: var(--ha-color-fill-disabled-quiet-resting, color-mix(in srgb, var(--cc-text) 12%, transparent));
    --cc-sw-off-line: var(--ha-color-border-neutral-normal, color-mix(in srgb, var(--cc-text) 40%, transparent));
    --cc-sw-off-thumb: var(--ha-color-on-neutral-normal, var(--cc-dim));
    --cc-sw-on: var(--ha-color-fill-primary-normal-resting, color-mix(in srgb, var(--cc-accent) 25%, var(--cc-bg)));
    --cc-sw-on-line: var(--ha-color-border-primary-loud, var(--cc-accent));
    --cc-sw-on-thumb: var(--ha-color-on-primary-normal, var(--cc-accent));
    --cc-range: var(--disabled-color, color-mix(in srgb, var(--cc-text) 15%, transparent));
    --cc-line: var(--divider-color, color-mix(in srgb, var(--cc-text) 12%, transparent));
    --cc-shadow: var(--shadow-color, rgba(0, 0, 0, 0.16));
    --cc-det: var(--state-binary_sensor-motion-on-color, var(--amber-color, #ffc107));
    /* glass over the picture: legible on day and IR night images alike */
    --cc-glass: rgba(18, 18, 18, 0.55);
    --cc-glass-hi: rgba(40, 40, 40, 0.75);
    --fs-xs: var(--ha-font-size-s, 12px);
    --fs-sm: var(--ha-font-size-m, 14px);
    --fs-md: var(--ha-font-size-l, 16px);
  }
  ha-card { overflow: hidden; }
  .cc { container-type: inline-size; }
  button { font: inherit; -webkit-tap-highlight-color: transparent; }

  /* Header in HA tile style; Cancel / Set overlay the title line's right end while staging */
  .hdr { padding: 12px 16px 10px; color: var(--cc-text); }
  .tline { position: relative; display: flex; align-items: center; min-height: 40px; min-width: 0; }
  .title { flex: 1 1 auto; display: flex; align-items: center; gap: 12px; min-width: 0; }
  .bwrap { position: relative; flex: none; }
  .htext { display: grid; min-width: 0; }
  .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-md); font-weight: 500; line-height: 1.25; }
  .hsub { display: flex; flex-wrap: wrap; align-items: baseline; column-gap: 5px; min-width: 0; height: 1.3em; overflow: hidden;
    white-space: nowrap; font-size: var(--fs-sm); line-height: 1.3; color: var(--cc-sub); font-variant-numeric: tabular-nums; }
  .hsub .g { display: inline-flex; align-items: baseline; gap: 5px; }
  .hsub .sep { font-style: normal; opacity: 0.8; }
  .hsub .st { color: var(--cc-text); font-weight: 500; }
  .hsub .st.live { color: color-mix(in srgb, var(--cc-live) 70%, var(--cc-text)); font-weight: 600; }
  .hsub .st.det { color: color-mix(in srgb, var(--cc-det) 45%, var(--cc-text)); font-weight: 600; }
  .hsub .st.staged { color: var(--cc-accent-text); font-weight: 600; }
  .hsub .st.pending { color: var(--cc-accent-text); font-style: italic; }
  .hsub .st.err { color: var(--cc-error-text); font-weight: 600; }
  .acts { position: absolute; right: 0; top: 50%; transform: translateY(-50%); display: flex; gap: 6px; padding-left: 10px;
    background: linear-gradient(90deg, transparent, var(--cc-bg) 10px); }
  .acts.off { visibility: hidden; }
  .staging .title { padding-right: 118px; }
  @container (max-width: 359px) {
    .staging .title { padding-right: 0; }
    .staging .htext { visibility: hidden; }
    .acts { left: 52px; justify-content: flex-end; background: none; padding-left: 0; }
  }
  .btn { min-height: 32px; padding: 0 14px; border-radius: 16px; cursor: pointer; border: 1px solid color-mix(in srgb, var(--cc-text) 16%, transparent);
    background: transparent; color: var(--cc-text); font-size: var(--fs-sm); font-weight: 500; }
  .btn:hover { background: color-mix(in srgb, var(--cc-text) 8%, transparent); }
  .btn:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 2px; }
  .btn.primary { border-color: transparent; background: var(--cc-sel); color: var(--cc-accent-text); }
  .btn.primary:hover { background: color-mix(in srgb, var(--cc-accent) 32%, var(--cc-bg)); }
  .btn.primary.warn { box-shadow: inset 0 0 0 1.5px var(--cc-warn); }
  .btn.icon { width: 32px; padding: 0; display: grid; place-items: center; --mdc-icon-size: 18px; }
  .btn .cnt { display: inline-block; min-width: 16px; margin-left: 6px; padding: 0 4px; border-radius: 8px; box-sizing: border-box;
    font-size: var(--fs-xs); line-height: 16px; text-align: center; background: color-mix(in srgb, var(--cc-accent) 30%, var(--cc-bg)); }
  .btn .cnt.none { visibility: hidden; }

  /* Camera button: grey idle, red with a glow while live, amber on a detection; click opens the action menu */
  .mbtn { position: relative; width: 40px; height: 40px; border-radius: 50%; border: 0; padding: 0; margin: 0; cursor: pointer;
    display: grid; place-items: center; --mdc-icon-size: 22px; --mc: var(--cc-idle);
    color: var(--mc); background: color-mix(in srgb, var(--mc) 20%, transparent); }
  .mbtn > ha-icon { color: inherit; }
  .mbtn:hover { background: color-mix(in srgb, var(--mc) 30%, transparent); }
  .mbtn:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 2px; }
  .mbtn.st-live { --mc: var(--cc-live); background: color-mix(in srgb, var(--mc) 38%, transparent); color: color-mix(in srgb, var(--mc) 80%, var(--cc-on-accent));
    box-shadow: 0 0 10px color-mix(in srgb, var(--mc) 45%, transparent); }
  .mbtn.st-det { --mc: var(--cc-det); }
  .mbtn.st-off { --mc: var(--cc-idle); opacity: 0.7; }
  .mbtn.staged { box-shadow: 0 0 0 2px var(--cc-accent); }
  .mbtn.failed { box-shadow: 0 0 0 2px var(--cc-error); }
  .badge { position: absolute; top: -3px; right: -3px; width: 18px; height: 18px; border-radius: 50%; display: grid; place-items: center;
    --mdc-icon-size: 12px; background: var(--cc-idle); box-shadow: 0 0 0 1.5px var(--cc-bg); }
  .badge ha-icon { display: flex; width: 12px; height: 12px; line-height: 0; color: var(--cc-on-accent); }
  .badge.live { background: var(--cc-live); }
  .badge.det { background: var(--cc-det); }
  .badge.det ha-icon { color: var(--black-color, #000); }
  /* Opens down over the picture, so it stays inside the card (ha-card clips its content) */
  .menu { position: absolute; top: calc(100% + 4px); left: 0; z-index: 6; min-width: 210px; padding: 4px; border-radius: 12px;
    background: var(--cc-bg); box-shadow: 0 4px 16px var(--cc-shadow); border: 1px solid var(--cc-line);
    display: grid; gap: 2px; }
  .mi { display: flex; align-items: center; gap: 10px; min-height: 36px; padding: 0 10px; border: 0; border-radius: 8px; cursor: pointer;
    background: transparent; color: var(--cc-text); font-size: var(--fs-sm); text-align: left; --mdc-icon-size: 18px; white-space: nowrap; }
  .mi.sep { margin-top: 3px; box-shadow: 0 -1px 0 color-mix(in srgb, var(--cc-text) 10%, transparent); border-top-left-radius: 0; border-top-right-radius: 0; }
  .mi ha-icon { color: var(--cc-sub); }
  .mi .chk { margin-left: auto; color: var(--cc-accent-text); }
  .mi:hover, .mi:focus-visible { background: color-mix(in srgb, var(--cc-text) 8%, transparent); outline: none; }
  .mi.sel { font-weight: 600; background: var(--cc-sel-quiet); }

  /* Picture: a fixed 16:9 frame, so cards side by side line up whatever each camera can do */
  /* The picture is its own size container: in full screen the overlay sizes follow the screen, not the card */
  .pic { position: relative; aspect-ratio: 16 / 9; overflow: hidden; background: #111; color: #fff; container-type: inline-size; }
  .pic:fullscreen { aspect-ratio: auto; width: 100%; height: 100%; background: #000; }
  .pic:fullscreen .stillimg { object-fit: contain; }
  .vslot, .still, .over { position: absolute; inset: 0; }
  .vslot ha-camera-stream, .vslot ha-web-rtc-player, .vslot ha-hls-player { display: block; width: 100%; height: 100%; }
  .vslot ha-web-rtc-player, .vslot ha-hls-player { --video-max-height: 100%; }
  .ctr.wake { gap: 4px; }
  .ctr .ws { margin-top: -2px; font-size: var(--fs-xs); opacity: 0.85; font-variant-numeric: tabular-nums; }
  /* Narrow pictures: spinner and seconds only (the header says "Starting live view"), clear of the chips and the bar */
  @container (max-width: 339px) { .ctr.wake .wt { display: none; } }
  .still { display: grid; place-items: center; transition: opacity 0.4s; }
  .still.dim { opacity: 0.45; }
  .still.gone { opacity: 0; pointer-events: none; }
  .stillimg { width: 100%; height: 100%; object-fit: cover; user-select: none; }
  .ovl { position: absolute; inset: 0; }
  /* A soft scrim at the top and bottom keeps the chips and controls legible */
  .ovl::before { content: ''; position: absolute; inset: 0; pointer-events: none;
    background: linear-gradient(180deg, rgba(0,0,0,0.38), transparent 26%, transparent 62%, rgba(0,0,0,0.42)); }
  .chip { position: absolute; display: flex; align-items: center; gap: 4px; max-width: calc(50% - 16px); height: 26px; padding: 0 9px 0 6px;
    box-sizing: border-box; border: 0; border-radius: 13px; white-space: nowrap; color: #fff; background: var(--cc-glass);
    backdrop-filter: blur(6px); font-size: var(--fs-xs); font-weight: 500; font-variant-numeric: tabular-nums; --mdc-icon-size: 16px; }
  button.chip { cursor: pointer; }
  button.chip:hover { background: var(--cc-glass-hi); }
  .chip span { overflow: hidden; text-overflow: ellipsis; }
  .chip ha-icon { display: flex; flex: none; width: 16px; height: 16px; line-height: 0; color: rgba(255, 255, 255, 0.85); }
  .chip:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
  .chip.tl { left: 10px; top: 10px; } .chip.tr { right: 10px; top: 10px; }
  /* Status and battery side by side at the top left: the cameras burn their clock into the top right */
  .crow { position: absolute; left: 10px; top: 10px; right: 10px; display: flex; gap: 6px; min-width: 0; pointer-events: none; }
  .crow > .chip { position: static; flex: none; max-width: 100%; pointer-events: auto; }
  .crow > .chip.tl { flex: 0 1 auto; min-width: 0; }
  .chip.hi ha-icon { color: var(--cc-bat-hi); } .chip.mid ha-icon { color: var(--cc-bat-mid); } .chip.lo ha-icon { color: var(--cc-bat-lo); }
  .chip.rec { gap: 6px; padding: 0 10px 0 9px; font-weight: 600; letter-spacing: 0.02em; }
  .chip.rec .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--cc-live); box-shadow: 0 0 6px var(--cc-live); animation: pulse 2s ease-in-out infinite; }
  .chip.rec .tv { font-weight: 500; opacity: 0.9; }
  @keyframes pulse { 50% { opacity: 0.35; } }

  /* Idle: one play button in the centre, continuous beside it */
  .ctr { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); display: grid; grid-template-columns: auto auto;
    align-items: center; justify-items: center; column-gap: 10px; row-gap: 6px; }
  .play { grid-column: 1; width: 56px; height: 56px; border-radius: 50%; border: 0; padding: 0; cursor: pointer; display: grid; place-items: center;
    background: var(--cc-glass); backdrop-filter: blur(6px); color: #fff; --mdc-icon-size: 34px; box-shadow: 0 0 0 1.5px rgba(255,255,255,0.55); }
  .play:hover { background: color-mix(in srgb, var(--cc-live) 55%, var(--cc-glass-hi)); }
  .pin0 { grid-column: 2; width: 34px; height: 34px; border-radius: 50%; border: 0; padding: 0; cursor: pointer; display: grid; place-items: center;
    background: var(--cc-glass); backdrop-filter: blur(6px); color: #fff; --mdc-icon-size: 18px; }
  .pin0:hover { background: var(--cc-glass-hi); }
  .play:focus-visible, .pin0:focus-visible, .lb:focus-visible, .pb:focus-visible, .zb:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
  .plbl { grid-column: 1; font-size: var(--fs-xs); font-weight: 500; text-shadow: 0 1px 3px rgba(0,0,0,0.8); font-variant-numeric: tabular-nums; }
  .ctr.off, .ctr.wake { display: flex; flex-direction: column; gap: 8px; font-size: var(--fs-sm); text-shadow: 0 1px 3px rgba(0,0,0,0.8); --mdc-icon-size: 40px; }
  .ctr.off ha-icon { color: rgba(255, 255, 255, 0.6); }
  .spin { width: 30px; height: 30px; border-radius: 50%; border: 3px solid rgba(255,255,255,0.25); border-top-color: #fff; animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Live: stop / continuous / sound / full screen, bottom left */
  .lbar { position: absolute; left: 10px; bottom: 10px; display: flex; gap: 2px; padding: 3px; border-radius: 19px;
    background: var(--cc-glass); backdrop-filter: blur(6px); }
  .lb { width: 32px; height: 32px; border-radius: 50%; border: 0; padding: 0; cursor: pointer; display: grid; place-items: center;
    background: transparent; color: #fff; --mdc-icon-size: 20px; }
  .lb:hover { background: rgba(255, 255, 255, 0.16); }
  .lb.on { background: color-mix(in srgb, var(--cc-accent) 70%, transparent); }
  .lb.rec ha-icon { color: color-mix(in srgb, var(--cc-live) 50%, #fff); }
  .lb.rec.on { background: color-mix(in srgb, var(--cc-live) 60%, transparent); cursor: default; }
  .lb.rec.on ha-icon { color: #fff; }
  .chip.recc { gap: 5px; }
  /* Narrow pictures: the recording chip keeps its dot and seconds */
  @container (max-width: 359px) { .chip.recc .rw { display: none; } }

  /* Pan/tilt pad and zoom pill, bottom right, on every state; the pad works only while the live picture shows */
  .ptzw { position: absolute; right: 10px; bottom: 10px; display: flex; align-items: flex-end; gap: 8px; }
  .ptz { position: relative; width: 96px; height: 96px; border-radius: 50%; background: var(--cc-glass); backdrop-filter: blur(6px);
    box-shadow: inset 0 0 0 1px rgba(255,255,255,0.14); }
  .pb { position: absolute; width: 32px; height: 32px; border: 0; padding: 0; border-radius: 50%; cursor: pointer; display: grid; place-items: center;
    background: transparent; color: #fff; --mdc-icon-size: 24px; }
  .pb:hover:not(:disabled) { background: rgba(255, 255, 255, 0.16); }
  .pb:active:not(:disabled) { background: rgba(255, 255, 255, 0.28); }
  .pb:disabled, .zb:disabled { opacity: 0.35; cursor: default; }
  .ptz.off { background: rgba(18, 18, 18, 0.35); }
  .pb.up { left: 32px; top: 2px; } .pb.down { left: 32px; bottom: 2px; }
  .pb.left { left: 2px; top: 32px; } .pb.right { right: 2px; top: 32px; }
  .pb.hbtn { left: 34px; top: 34px; width: 28px; height: 28px; --mdc-icon-size: 16px; background: rgba(255,255,255,0.1); }
  .ptz .hub { position: absolute; left: 44px; top: 44px; width: 8px; height: 8px; border-radius: 50%; background: rgba(255,255,255,0.3); }
  .busy { animation: busy 0.9s ease-in-out infinite; }
  /* Presses waiting behind the running move */
  .qn { position: absolute; right: -2px; top: -2px; min-width: 14px; height: 14px; padding: 0 3px; box-sizing: border-box; border-radius: 7px;
    background: var(--cc-accent); color: var(--cc-on-accent); font: 600 10px/14px sans-serif; font-style: normal; text-align: center; pointer-events: none; }
  @keyframes busy { 50% { background: rgba(255, 255, 255, 0.3); } }
  .zp { display: flex; flex-direction: column; align-items: center; width: 34px; padding: 1px 0; border-radius: 17px;
    background: var(--cc-glass); backdrop-filter: blur(6px); box-shadow: inset 0 0 0 1px rgba(255,255,255,0.14); }
  .zb { width: 32px; height: 32px; border: 0; padding: 0; border-radius: 50%; cursor: pointer; display: grid; place-items: center;
    background: transparent; color: #fff; --mdc-icon-size: 20px; }
  .zb:hover:not(:disabled) { background: rgba(255, 255, 255, 0.16); }
  .zv { font-size: var(--fs-xs); font-weight: 600; line-height: 16px; font-variant-numeric: tabular-nums; }
  .zv.pend { opacity: 0.7; }
  .zp.zoomed .zv { color: color-mix(in srgb, var(--cc-accent) 55%, #fff); }
  .zr { display: flex; align-items: center; justify-content: center; gap: 1px; width: 30px; height: 22px; margin: 1px 0 2px; padding: 0;
    border: 0; border-top: 1px solid rgba(255,255,255,0.16); border-radius: 0 0 14px 14px; cursor: pointer; background: transparent; color: #fff;
    font: 600 10px/1 sans-serif; --mdc-icon-size: 12px; }
  .zr ha-icon { display: flex; width: 12px; height: 12px; line-height: 0; }
  .zr:hover { background: rgba(255, 255, 255, 0.16); }
  .zr:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }
  /* Presets over the live picture: a row of thumbnails above the bar, sideways scrolling when they do not fit */
  .lpre { position: absolute; left: 10px; bottom: 54px; display: flex; gap: 6px; max-width: calc(100% - 180px); padding: 4px;
    overflow-x: auto; border-radius: 10px; background: var(--cc-glass); backdrop-filter: blur(6px); scrollbar-width: none; }
  .lp { position: relative; flex: none; width: 76px; aspect-ratio: 16 / 9; border: 0; padding: 0; border-radius: 6px; overflow: hidden; cursor: pointer;
    display: grid; place-items: center; background: rgba(255,255,255,0.12); color: #fff; --mdc-icon-size: 18px; }
  .lp img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  .lp .pn { position: absolute; left: 3px; bottom: 3px; display: flex; align-items: center; gap: 2px; padding: 0 5px; height: 16px; border-radius: 8px;
    background: var(--cc-glass); color: #fff; font-size: 11px; font-weight: 600; --mdc-icon-size: 11px; }
  .lp .pn ha-icon { display: flex; width: 11px; height: 11px; line-height: 0; }
  .lp:hover { box-shadow: inset 0 0 0 2px rgba(255,255,255,0.6); }
  .lp.at { box-shadow: inset 0 0 0 2px var(--cc-accent); }
  .lp.busy { animation: none; box-shadow: inset 0 0 0 2px #fff; }
  .lp:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }
  /* Full screen on a large display: the overlay controls grow with the picture */
  @container (min-width: 900px) { .crow, .lbar, .ptzw, .lpre, .toast { zoom: 1.35; } }
  .toast { position: absolute; left: 10px; top: 10px; display: flex; align-items: center; gap: 5px; max-width: calc(100% - 20px); height: 26px;
    padding: 0 10px 0 7px; border-radius: 13px; box-sizing: border-box; background: color-mix(in srgb, var(--cc-error) 85%, #000); color: #fff;
    font-size: var(--fs-xs); font-weight: 500; white-space: nowrap; box-shadow: 0 2px 8px rgba(0,0,0,0.35); --mdc-icon-size: 16px; }
  .toast ha-icon { display: flex; flex: none; width: 16px; height: 16px; line-height: 0; }
  .toast span { overflow: hidden; text-overflow: ellipsis; }
  /* Narrow pictures: a smaller pad and bar so both fit side by side */
  @container (max-width: 339px) {
    .ptz { width: 80px; height: 80px; }
    .pb { width: 26px; height: 26px; --mdc-icon-size: 20px; }
    .pb.up { left: 27px; } .pb.down { left: 27px; } .pb.left, .pb.right { top: 27px; }
    .pb.hbtn { left: 28px; top: 28px; width: 24px; height: 24px; }
    .ptz .hub { left: 36px; top: 36px; }
    .zp { width: 30px; } .zb { width: 28px; height: 28px; }
    .lb { width: 28px; height: 28px; --mdc-icon-size: 18px; }
    .lbar, .ptzw { bottom: 8px; } .lbar { left: 8px; } .ptzw { right: 8px; gap: 6px; }
    .zr { width: 26px; } .lpre { left: 8px; bottom: 46px; max-width: calc(100% - 140px); } .lp { width: 64px; }
  }
  /* A picture too narrow for the whole bar (with Record) beside the pan/tilt controls: continuous and full screen
     stay in the header menu (Keep live / Full screen) */
  @container (max-width: 389px) {
    .has-ptz .lbar .lb.opt2 { display: none; }
  }
  /* Narrow pictures (phones): the pan/tilt pad only while the live picture shows (greyed otherwise) */
  @container (max-width: 459px) {
    .ovl:not(.aim) .ptz { display: none; }
  }
  /* Live picture, every width and full screen: CTL_HIDE_MS after the last touch or click (class quiet) the controls,
     the battery chip and the scrim fade and the LIVE chip shrinks to its red dot; the REC chip and messages stay.
     Keyboard focus inside or a mouse resting on a shown control keeps them. */
  .lbar, .ptzw, .lpre, .crow > .chip, .ovl::before { transition: opacity 0.25s; }
  .quiet:not(:has(:focus-visible, .lbar:hover, .ptzw:hover, .lpre:hover, button.chip:hover)) :is(.lbar, .ptzw, .lpre, .crow > .chip:not(.rec)),
  .quiet:not(:has(:focus-visible, .lbar:hover, .ptzw:hover, .lpre:hover, button.chip:hover)) .ovl::before { opacity: 0; pointer-events: none; }
  .quiet:not(:has(:focus-visible, .lbar:hover, .ptzw:hover, .lpre:hover, button.chip:hover)) .chip.live { padding: 0 9px; background: none; backdrop-filter: none; }
  .quiet:not(:has(:focus-visible, .lbar:hover, .ptzw:hover, .lpre:hover, button.chip:hover)) .chip.live > span { display: none; }
  .quiet:not(:has(:focus-visible, .lbar:hover, .ptzw:hover, .lpre:hover, button.chip:hover)) .chip.live .dot { box-shadow: 0 0 0 2px rgba(0, 0, 0, 0.35), 0 0 6px var(--cc-live); }
  /* alone on the picture the dot pulses less deep, so it stays clearly red */
  @media (prefers-reduced-motion: no-preference) { .quiet:not(:has(:focus-visible, .lbar:hover, .ptzw:hover, .lpre:hover, button.chip:hover)) .chip.live .dot { animation-name: dot-pulse; } }
  @keyframes dot-pulse { 50% { opacity: 0.7; } }

  /* Section tabs, the first level: a segmented control with equal widths and fixed labels, so a tab stays under
     the pointer */
  .secs { display: flex; gap: 2px; margin: 8px 10px; padding: 2px; border-radius: 10px;
    background: var(--cc-track); container: secs / inline-size; }
  .sec { position: relative; flex: 1 1 0; display: flex; align-items: center; justify-content: center; gap: 6px; min-width: 0; height: 30px;
    padding: 0 8px; border: 0; border-radius: 8px; background: transparent; color: var(--cc-sub); font-size: var(--fs-sm); font-weight: 500;
    cursor: pointer; --mdc-icon-size: 18px; }
  .sec ha-icon { display: flex; flex: none; width: 18px; height: 18px; line-height: 0; }
  .sec span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sec:hover { color: var(--cc-text); background: color-mix(in srgb, var(--cc-text) 6%, transparent); }
  .sec:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .sec.on { background: var(--cc-sel); color: var(--cc-text); font-weight: 600; }
  .sec.err { color: var(--cc-error-text); }
  @container secs (max-width: 219px) { .sec ha-icon { display: none; } }

  /* Settings rows: icon, name, status word and value; the control full width under it */
  .ctl-rows { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); align-items: start; gap: 4px 16px; padding: 0 10px 10px; }
  .row { display: grid; grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "name val" "ctl ctl";
    align-items: center; column-gap: 8px; padding: 2px 6px 4px; border-radius: 10px; border: 1px solid transparent; --ctl-accent: var(--cc-accent); }
  .row.staged { background: color-mix(in srgb, var(--cc-accent) 6%, transparent); --ctl-accent: var(--cc-accent); }
  .row.failed { border-color: var(--cc-error); }
  .rname { grid-area: name; display: flex; align-items: center; gap: 7px; min-width: 0; min-height: 26px; --mdc-icon-size: 18px; }
  .rname ha-icon { flex: none; color: var(--cc-sub); }
  .rn { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-sm); line-height: 1.2; }
  .rsub { flex: none; font-size: var(--fs-xs); line-height: 1.2; color: var(--cc-sub); white-space: nowrap; }
  .rsub.err { color: var(--cc-error-text); font-weight: 500; }
  .rsub.warn { color: color-mix(in srgb, var(--cc-warn) 55%, var(--cc-text)); font-weight: 500; }
  .rval { grid-area: val; font-size: var(--fs-sm); font-weight: 500; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .row.staged .rval { color: var(--cc-accent-text); font-weight: 700; }
  .row.pending .rval { color: var(--cc-sub); font-style: italic; }
  .row.failed .rval { color: var(--cc-error-text); }
  .rctl { grid-area: ctl; position: relative; display: flex; align-items: center; height: 32px; min-width: 0; }
  .seg { display: grid; grid-auto-flow: column; grid-auto-columns: 1fr; gap: 2px; width: 100%; height: 30px; padding: 2px;
    box-sizing: border-box; border-radius: 9px; background: var(--cc-track); }
  .opt { display: flex; align-items: center; justify-content: center; gap: 5px; min-width: 0; padding: 0 6px; border: 1px solid transparent;
    border-radius: 7px; background: transparent; color: var(--cc-sub); font-size: var(--fs-xs); cursor: pointer; --mdc-icon-size: 16px; }
  .opt.txt { padding: 0 2px; }
  .opt span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .opt:hover { color: var(--cc-text); background: color-mix(in srgb, var(--cc-text) 7%, transparent); }
  .opt:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .opt[aria-pressed="true"] { background: var(--cc-sel); color: var(--cc-text); font-weight: 500; }
  .row.staged .opt[aria-pressed="true"] { border-color: var(--cc-accent); color: var(--cc-accent-text); }
  /* Icon options show only their icon on narrow rows (the label stays in aria-label) */
  @container (max-width: 359px) { .seg .opt ha-icon:has(+ span) { display: none; } }
  .ctl-range { -webkit-appearance: none; appearance: none; display: block; width: 100%; height: 32px; margin: 0; background: transparent; cursor: pointer; --pos: 50%; }
  .ctl-range:focus { outline: none; }
  .ctl-range::-webkit-slider-runnable-track { height: 4px; border-radius: 2px;
    background: linear-gradient(to right, var(--ctl-accent) var(--pos), var(--cc-range) var(--pos)); }
  .ctl-range::-moz-range-track { height: 4px; border-radius: 2px; background: var(--cc-range); }
  .ctl-range::-moz-range-progress { height: 4px; border-radius: 2px; background: var(--ctl-accent); }
  .ctl-range::-webkit-slider-thumb { -webkit-appearance: none; width: 18px; height: 18px; margin-top: -7px;
    border: 2px solid var(--cc-bg); border-radius: 50%; background: var(--ctl-accent); box-shadow: 0 1px 3px var(--cc-shadow); }
  .ctl-range::-moz-range-thumb { width: 14px; height: 14px; border: 2px solid var(--cc-bg); border-radius: 50%; background: var(--ctl-accent); }
  .ctl-range:focus-visible::-webkit-slider-thumb { box-shadow: 0 0 0 3px color-mix(in srgb, var(--cc-accent) 45%, transparent); }
  .ghost { position: absolute; top: 50%; left: calc(9px + (100% - 18px) * var(--at)); width: 22px; height: 22px; margin: -11px 0 0 -11px;
    padding: 0; border: 0; background: transparent; cursor: pointer; z-index: 1; display: grid; place-items: center; }
  .ghost i { width: 10px; height: 10px; border-radius: 50%; background: color-mix(in srgb, var(--cc-text) 38%, var(--cc-bg)); }
  .ghost:focus-visible { outline: 2px solid var(--cc-accent); border-radius: 50%; }

  /* Group headings span the row grid */
  .ghead { grid-column: 1 / -1; display: flex; align-items: center; gap: 6px; padding: 8px 6px 0; color: var(--cc-sub);
    font-size: var(--fs-xs); font-weight: 600; --mdc-icon-size: 16px; }
  .ghead:first-child { padding-top: 0; }
  .ghead ha-icon { display: flex; width: 16px; height: 16px; line-height: 0; }
  /* A setting that does not apply now: greyed out, its reason in the status word */
  /* Names and values of a greyed row stay readable (secondary text); its icon and control are faded */
  .row.na .rn, .row.na .rval { color: var(--cc-dim); }
  .row.na .rctl, .row.na .rname ha-icon { opacity: 0.45; }
  .opt:disabled, .dd:disabled, .ctl-range:disabled { cursor: default; }
  .opt:disabled:hover { color: var(--cc-sub); background: transparent; }
  /* Dropdown: the value on a button, the list over the rows below (or above) */
  .row.ddopen { position: relative; z-index: 2; }
  .dd { display: flex; align-items: center; justify-content: space-between; gap: 6px; width: 100%; height: 30px; padding: 0 4px 0 10px;
    box-sizing: border-box; border: 1px solid var(--cc-field-line); border-radius: 9px; cursor: pointer;
    background: var(--cc-field); color: var(--cc-text); font-size: var(--fs-xs); font-weight: 500; --mdc-icon-size: 20px; }
  .dd span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dd ha-icon { display: flex; flex: none; width: 20px; height: 20px; line-height: 0; color: var(--cc-sub); }
  .dd:hover:not(:disabled) { background: color-mix(in srgb, var(--cc-text) 9%, transparent); }
  .dd:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .row.staged .dd { border-color: var(--cc-accent); color: var(--cc-accent-text); font-weight: 600; }
  .ddl { position: absolute; left: 0; right: 0; top: calc(100% + 2px); z-index: 7; display: grid; gap: 1px; padding: 4px; overflow-y: auto;
    border-radius: 10px; background: var(--cc-bg); box-shadow: 0 4px 16px var(--cc-shadow); border: 1px solid var(--cc-line);
    overscroll-behavior: contain; }
  .ddl.up { top: auto; bottom: calc(100% + 2px); }
  .ddo { display: flex; align-items: center; gap: 8px; min-height: 32px; padding: 0 8px; border: 0; border-radius: 7px; cursor: pointer;
    background: transparent; color: var(--cc-text); font-size: var(--fs-sm); text-align: left; --mdc-icon-size: 16px; }
  .ddo span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ddo:hover, .ddo:focus-visible { background: color-mix(in srgb, var(--cc-text) 8%, transparent); outline: none; }
  .ddo[aria-selected="true"] { font-weight: 600; background: var(--cc-sel-quiet); }
  .ddo .chk { display: flex; width: 16px; height: 16px; line-height: 0; margin-left: auto; color: var(--cc-accent-text); }
  .row.ddrow .rval { display: none; }
  /* Switch rows: name and toggle on one line */
  .row.tgrow { grid-template-areas: "name val"; padding-bottom: 2px; }
  .ctl-rows > .row.tg1 { grid-column-start: 1; }
  .tgrow .rn, .rn { white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  /* A narrow row shows a segment with long option texts as icons only (the text stays in aria-label and the value) */
  .row { container: srow / inline-size; }
  @container srow (max-width: 319px) { .seg.long .opt ha-icon:has(+ span) { display: none; } }
  @container srow (max-width: 239px) { .seg.long .opt ha-icon:has(+ span) { display: flex; } .seg.long .opt ha-icon + span { display: none; } }
  .tg { grid-area: val; position: relative; width: 36px; height: 20px; padding: 0; box-sizing: border-box; border: 1px solid var(--cc-sw-off-line);
    border-radius: 10px; cursor: pointer; background: var(--cc-sw-off); transition: background 0.15s; }
  .tg i { position: absolute; left: 2px; top: 2px; width: 14px; height: 14px; border-radius: 50%; background: var(--cc-sw-off-thumb);
    box-shadow: 0 1px 2px var(--cc-shadow); transition: left 0.15s; }
  .tg.on { background: var(--cc-sw-on); border-color: var(--cc-sw-on-line); }
  .tg.on i { left: 18px; background: var(--cc-sw-on-thumb); }
  .tg.unk i { left: 10px; opacity: 0.6; }
  .row.staged .tg { box-shadow: 0 0 0 2px var(--cc-accent); }
  .row.pending .tg { opacity: 0.6; }
  .row.failed .tg { box-shadow: 0 0 0 2px var(--cc-error); }
  .tg:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 2px; }
  .tg:disabled { cursor: default; }
  .row.na .tg { opacity: 0.45; }

  .compact .ctl-rows { grid-template-columns: minmax(0, 1fr); }
  /* A controller and the settings that apply only in one of its states: full width, the dependents indented
     under it on a rule (they keep their place in every state, greyed when they do not apply) */
  .cgrp { grid-column: 1 / -1; display: grid; gap: 2px; }
  .deps { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); align-items: start; gap: 2px 16px;
    margin: 0 0 4px 15px; padding-left: 10px; border-left: 2px solid color-mix(in srgb, var(--cc-text) 14%, transparent); }
  .compact .deps { grid-template-columns: minmax(0, 1fr); }
  .dcap { grid-column: 1 / -1; padding: 0 6px; font-size: var(--fs-xs); line-height: 16px; color: var(--cc-sub); }
  .deps.on { border-left-color: color-mix(in srgb, var(--cc-accent) 55%, transparent); }
  .row.unsure { border-color: var(--cc-warn); }
  .row.unsure .rsub { color: color-mix(in srgb, var(--cc-warn) 55%, var(--cc-text)); font-weight: 500; }
  .rval.unrep { color: var(--cc-sub); font-weight: 400; }
  /* A segment shows its value itself; the value text stays when nothing is selected or only icons show */
  .row.segrow:not(.segunk) .rval { display: none; }
  @container srow (max-width: 319px) { .row.segrow.segicons .rval { display: inline; } }
  /* A slider without a value: the thumb only hints where it would start */
  .ctl-range.unk::-webkit-slider-thumb { opacity: 0.35; }
  .ctl-range.unk::-moz-range-thumb { opacity: 0.35; }
  /* Sub-tabs, the second level (Settings groups, History kinds): an underlined row inside the open section, smaller
     and without a track or filled segment; the open one in the accent text colour on a 2 px rule. Icons only on a
     narrow card except the open tab. */
  .stabs { grid-column: 1 / -1; display: flex; gap: 2px 0; margin: 0 0 6px; padding: 2px 0 0; container: stabs / inline-size; }
  .stab { position: relative; flex: 1 1 auto; display: flex; align-items: center; justify-content: center; gap: 4px; min-width: 0; height: 32px;
    padding: 0 8px; border: 0; border-radius: 6px 6px 0 0; background: transparent; color: var(--cc-sub); font-size: var(--fs-xs); font-weight: 400;
    cursor: pointer; box-shadow: inset 0 -1px 0 var(--cc-line); --mdc-icon-size: 16px; }
  .stab ha-icon { display: flex; flex: none; width: 16px; height: 16px; line-height: 0; }
  .stab span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .stab:hover { color: var(--cc-text); background: color-mix(in srgb, var(--cc-text) 5%, transparent); }
  .stab:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -2px; }
  .stab.on { color: var(--cc-accent-text); font-weight: 600; box-shadow: inset 0 -2px 0 var(--cc-accent-text); flex: 1 0 auto; }
  .stab.err { color: var(--cc-error-text); }
  .stab.on.err { box-shadow: inset 0 -2px 0 var(--cc-error); }
  .tdot { position: absolute; top: 4px; right: 5px; width: 6px; height: 6px; border-radius: 50%; background: var(--cc-accent); display: none; }
  .stab.stg .tdot, .sec.stg .tdot { display: block; }
  .stab.err .tdot, .sec.err .tdot { display: block; background: var(--cc-error); }
  .stabs { flex-wrap: wrap; }
  .stitle { display: none; flex: 1 0 100%; padding: 4px 6px 0; font-size: var(--fs-xs); font-weight: 600; color: var(--cc-sub); }
  @container stabs (max-width: 459px) { .stab:not(.on) span { display: none; } .stab:not(.on) { flex: 0 0 38px; padding: 0; } }
  @container stabs (max-width: 299px) { .stab span { display: none; } .stab, .stab.on { flex: 1 1 0; min-width: 0; padding: 0; } .stitle { display: block; } }
  /* Power readings: chips that open the reading's history */
  .pstats { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 4px 6px; padding: 2px 6px 4px; }
  .pst { display: flex; align-items: center; gap: 4px; height: 24px; padding: 0 8px 0 6px; border: 0; border-radius: 12px; cursor: pointer;
    background: color-mix(in srgb, var(--cc-text) 6%, transparent); color: var(--cc-text); font-size: var(--fs-xs); --mdc-icon-size: 15px; }
  .pst ha-icon { display: flex; width: 15px; height: 15px; line-height: 0; color: var(--cc-sub); }
  .pst:hover { background: color-mix(in srgb, var(--cc-text) 10%, transparent); }
  .pst:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .chip .sun { color: var(--cc-sun); }
  /* History: one scrolling row of saved stills, newest first; images load as they scroll into view */
  .hist { padding: 0 10px 12px; container: hist / inline-size; --ht-w: 120px; }
  .compact .hist { --ht-w: 104px; }
  /* A clock with an AM/PM marker needs wider tiles for "28.09 10:05 PM" */
  .hist.wide, .compact .hist.wide { --ht-w: 148px; }
  /* Loading and empty take the tile row's height */
  .hist > .hmsg { min-height: calc(var(--ht-w) * 9 / 16 + 4px); }
  /* History sub-tabs (the settings tabs' second-level look) and the Pictures / Videos filter on one line */
  .hbar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .htabs { flex: 1 1 auto; min-width: 0; margin: 0; container: htabs / inline-size; }
  .hfilt { flex: none; display: flex; gap: 2px; padding: 2px; border-radius: 10px; background: var(--cc-track); }
  .hf { display: flex; align-items: center; gap: 3px; height: 30px; padding: 0 8px 0 6px; border: 0; border-radius: 8px; cursor: pointer;
    background: transparent; color: var(--cc-sub); font-size: var(--fs-xs); font-variant-numeric: tabular-nums; --mdc-icon-size: 16px; }
  .hf ha-icon { display: flex; width: 16px; height: 16px; line-height: 0; }
  .hf.on { background: var(--cc-sel); color: var(--cc-text); font-weight: 600; }
  .hf:not(.on) ha-icon { opacity: 0.6; }
  .hf[aria-disabled="true"] { cursor: default; }
  .hf:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .hf:hover:not(.on) { color: var(--cc-text); }
  /* Count buttons and Station's count and refresh share one width (counts up to 3 digits), so the sub-tab bar keeps
     its boxes when Station opens */
  .hf[data-hf], .hf.stc, .hf.srf { min-width: 54px; box-sizing: border-box; justify-content: center; }
  .hf.hday { min-width: 32px; justify-content: center; padding: 0 7px; }
  .hf.hdx { width: 26px; justify-content: center; padding: 0; }
  /* A set day fills the calendar; its date shows from 460 px (the tiles and rows carry dates). Under 380 px no ✕
     either (the picker's Clear goes back), so the bar keeps its width */
  @container hist (max-width: 459px) { .hf.hday span { display: none; } }
  @container hist (max-width: 379px) { .hf.hdx { display: none; } }
  /* The browser's date picker where HA's is not loaded: opened over the day button, never shown itself */
  .hdn { position: fixed; width: 1px; height: 1px; padding: 0; border: 0; opacity: 0; pointer-events: none; }
  @container htabs (max-width: 359px) { .htabs .stab:not(.on) span { display: none; } .htabs .stab:not(.on) { flex: 0 0 38px; padding: 0; } }
  /* Narrow: tighter filter buttons and sub-tab icons, so the day button fits beside them */
  @container hist (max-width: 359px) { .hf { padding: 0 5px 0 4px; } .hf.hday { min-width: 28px; padding: 0 4px; } .htabs .stab:not(.on) { flex-basis: 32px; } }
  @container hist (max-width: 299px) { .hbar { flex-wrap: wrap; } .hfilt { margin-left: auto; } .htabs { flex-basis: 100%; } }
  .ht.vid .hph { position: absolute; inset: 0; display: grid; place-items: center; color: var(--cc-sub); --mdc-icon-size: 28px; }
  .ht .hvb { position: absolute; right: 4px; top: 4px; width: 20px; height: 20px; border-radius: 50%; display: grid; place-items: center;
    background: var(--cc-glass); color: #fff; --mdc-icon-size: 14px; }
  .ht .hvb ha-icon { display: flex; width: 14px; height: 14px; line-height: 0; }
  /* A history video keeps its own controls: the overlay lets presses through except on its chip and close button */
  .hvid { object-fit: contain; background: #000; }
  .ovl.hvo { pointer-events: none; }
  .ovl.hvo::before { display: none; }
  .ovl.hvo > * { pointer-events: auto; }
  .hstrip { display: grid; grid-auto-flow: column; grid-auto-columns: var(--ht-w); gap: 8px; overflow-x: auto; overscroll-behavior-x: contain;
    scroll-snap-type: x proximity; padding-bottom: 4px; scrollbar-width: thin; }
  .ht { position: relative; aspect-ratio: 16 / 9; border: 0; padding: 0; border-radius: 8px; overflow: hidden; cursor: pointer; scroll-snap-align: start;
    background: color-mix(in srgb, var(--cc-text) 10%, var(--cc-bg)); }
  .ht img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; user-select: none; }
  .ht .hl { position: absolute; left: 4px; bottom: 4px; display: flex; align-items: center; gap: 3px; max-width: calc(100% - 8px); height: 18px; padding: 0 6px 0 4px;
    box-sizing: border-box; border-radius: 9px; background: var(--cc-glass); color: #fff; font-size: var(--fs-xs); font-weight: 600;
    font-variant-numeric: tabular-nums; white-space: nowrap; --mdc-icon-size: 12px; }
  .ht .hl span { overflow: hidden; text-overflow: ellipsis; }
  .ht .hl ha-icon { display: flex; flex: none; width: 12px; height: 12px; line-height: 0; }
  .ht:hover { box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--cc-accent) 60%, transparent); }
  .ht.on { box-shadow: 0 0 0 2px var(--cc-accent); }
  .ht:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 2px; }
  .hmsg { min-height: 63px; display: grid; place-items: center; color: var(--cc-sub); font-size: var(--fs-sm); }
  /* Show more: the strip's last tile, same size as a picture tile */
  .hmore { aspect-ratio: 16 / 9; border: 0; padding: 0; border-radius: 8px; cursor: pointer; scroll-snap-align: start; display: grid;
    place-content: center; justify-items: center; gap: 2px; background: color-mix(in srgb, var(--cc-text) 6%, var(--cc-bg)); color: var(--cc-text);
    font: inherit; font-size: var(--fs-xs); font-weight: 600; --mdc-icon-size: 20px; }
  .hmore ha-icon { display: flex; width: 20px; height: 20px; line-height: 0; color: var(--cc-accent-text); }
  .hmore:hover { background: color-mix(in srgb, var(--cc-text) 12%, var(--cc-bg)); }
  .hmore:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 2px; }
  /* Station: a list of fixed height (loading, error, empty and rows alike), rows scroll inside it */
  .hf.stc { cursor: default; padding: 0 4px; }
  /* As wide as a filter button, so the sub-tab bar keeps its width when Station opens */
  .hf.srf { padding: 0; }
  /* Narrow: counts up to 2 digits keep the width */
  @container hist (max-width: 359px) { .hf[data-hf], .hf.stc, .hf.srf { min-width: 42px; } }
  .hf.ld ha-icon { animation: spin 1s linear infinite; }
  .slist { height: 176px; overflow-y: auto; overscroll-behavior: contain; display: grid; align-content: start; gap: 2px; scrollbar-width: thin; }
  .slist.msg { align-content: stretch; }
  .slist .hmsg { display: flex; align-items: center; justify-content: center; gap: 8px; padding: 0 12px; text-align: center; --mdc-icon-size: 18px; }
  .srow { display: grid; grid-template-columns: 64px minmax(0, 1fr) auto auto; align-items: center; gap: 4px 8px; min-height: 44px; padding: 3px 4px;
    border-radius: 8px; }
  .srow.on { background: var(--cc-sel-quiet); }
  .sth { position: relative; width: 64px; aspect-ratio: 16 / 9; border-radius: 6px; overflow: hidden; display: grid; place-items: center;
    background: color-mix(in srgb, var(--cc-text) 10%, var(--cc-bg)); color: var(--cc-sub); --mdc-icon-size: 18px; }
  .sth img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; user-select: none; }
  .sth img:not([src]) { visibility: hidden; }
  .stx { display: grid; gap: 2px; min-width: 0; }
  .sl1, .sl2 { display: flex; align-items: center; gap: 6px; min-width: 0; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .sl1 { font-size: var(--fs-sm); line-height: 1.25; }
  .swhen { min-width: 0; overflow: hidden; text-overflow: ellipsis; font-weight: 500; }
  .sdur { flex: none; color: var(--cc-sub); }
  .sl2 { font-size: var(--fs-xs); line-height: 18px; color: var(--cc-sub); }
  .skind { display: inline-flex; align-items: center; gap: 3px; min-width: 0; height: 18px; padding: 0 6px 0 4px; border-radius: 9px;
    background: color-mix(in srgb, var(--cc-text) 7%, transparent); color: var(--cc-text); --mdc-icon-size: 12px; }
  .skind ha-icon { display: flex; flex: none; width: 12px; height: 12px; line-height: 0; color: var(--cc-sub); }
  .skind span { overflow: hidden; text-overflow: ellipsis; }
  .sst { flex: none; }
  /* Show more: the station list's last row */
  .srow.smore { display: block; min-height: 0; }
  .smb { width: 100%; height: 36px; display: flex; align-items: center; justify-content: center; gap: 6px; border: 0; border-radius: 8px;
    cursor: pointer; background: color-mix(in srgb, var(--cc-text) 6%, transparent); color: var(--cc-text); font: inherit; font-size: var(--fs-sm);
    font-weight: 500; --mdc-icon-size: 18px; }
  .smb ha-icon { display: flex; width: 18px; height: 18px; line-height: 0; color: var(--cc-accent-text); }
  .smb:hover:not([aria-disabled="true"]) { background: color-mix(in srgb, var(--cc-text) 12%, transparent); }
  .smb[aria-disabled="true"] { cursor: progress; }
  .smb:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -2px; }
  .sst.ok { color: var(--cc-accent-text); font-weight: 500; }
  .sb { width: 34px; height: 34px; border: 0; padding: 0; border-radius: 50%; cursor: pointer; display: grid; place-items: center;
    background: color-mix(in srgb, var(--cc-text) 7%, transparent); color: var(--cc-text); --mdc-icon-size: 18px; }
  .sb ha-icon { display: flex; width: 18px; height: 18px; line-height: 0; }
  .sb:hover:not(:disabled):not([aria-disabled="true"]) { background: color-mix(in srgb, var(--cc-text) 13%, transparent); }
  .sb:disabled { opacity: 0.4; cursor: default; }
  .sb[aria-disabled="true"] { cursor: progress; }
  .sb:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 1px; }
  .mi:disabled { cursor: default; color: var(--cc-sub); }
  .mi:disabled:hover { background: transparent; }
  .spin.sm { width: 14px; height: 14px; border-width: 2px; border-color: color-mix(in srgb, var(--cc-text) 22%, transparent); border-top-color: var(--cc-text); }
  @container hist (max-width: 299px) { .srow { grid-template-columns: 44px minmax(0, 1fr) auto auto; gap: 4px 6px; } .sth { width: 44px; }
    .sb { width: 30px; height: 30px; } .sl2 { gap: 4px; } }
  /* Very narrow: no thumbnail, the text and both buttons keep their room */
  @container hist (max-width: 259px) { .srow { grid-template-columns: minmax(0, 1fr) auto auto; } .sth { display: none; } }
  .hview { z-index: 0; background: #111; }
  .chip.hchip { max-width: calc(100% - 58px); }
  .hx { position: absolute; right: 10px; top: 10px; width: 26px; height: 26px; border: 0; padding: 0; border-radius: 50%; cursor: pointer;
    display: grid; place-items: center; background: var(--cc-glass); backdrop-filter: blur(6px); color: #fff; --mdc-icon-size: 16px; }
  .hx:hover { background: var(--cc-glass-hi); }
  .hx:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
  .compact .mi { min-height: 32px; }
  @media (prefers-reduced-motion: reduce) { .still, .tg, .tg i, .lbar, .ptzw, .lpre, .crow > .chip, .ovl::before { transition: none; } .chip.rec .dot, .busy, .spin, .hf.ld ha-icon { animation: none; } }
`;

// ================== EDITOR ==================
const EDITOR_SCHEMA = [
  { name: 'entity', selector: { entity: { filter: [{ domain: 'camera', integration: DOMAIN }] } } },
  { name: 'name', selector: { entity_name: {} }, context: { entity: 'entity' } },
  { name: 'auto_live', selector: { select: { mode: 'dropdown', options: [
    { value: 'off', label: 'Off: start by hand' }, { value: 'timed', label: 'Timed live view' }, { value: 'continuous', label: 'Continuous live view' }] } } },
  { name: 'live_seconds', selector: { number: { min: 30, max: 900, step: 10, mode: 'box', unit_of_measurement: 's' } } },
  { name: 'history_folder', selector: { text: {} } },
  { name: 'layout', selector: { select: { mode: 'dropdown', options: [
    { value: 'auto', label: 'Auto (compact below 400 px)' }, { value: 'compact', label: 'Compact' }, { value: 'regular', label: 'Regular' }] } } },
];
const EDITOR_LABELS = { entity: 'Camera', name: 'Name', history_folder: 'History folder', auto_live: 'Start live view when the dashboard opens', live_seconds: 'Live view length', layout: 'Layout',
  settings_include: 'Settings to add', settings_exclude: 'Hidden settings', settings_groups: 'Settings groups' };
// Group names in the editor (the tab names, More and Other said in full)
const GROUP_EDITOR_NAMES = { more: 'More settings', other: 'Other (settings to add)' };
// Settings tabs as HA's reorderable multi-select: a chip per shown group (drag to reorder, ✕ to hide), the rest to add
const GROUPS_FIELD = { name: 'settings_groups', selector: { select: { multiple: true, reorder: true,
  options: GROUP_IDS.map(id => ({ value: id, label: GROUP_EDITOR_NAMES[id] || groupInfo(id)[1] })) } } };
const EDITOR_HELPERS = {
  history_folder: 'The camera\'s folder under Media › eufy_home_security; only when the camera was renamed after its stills were saved',
  auto_live: 'Wakes the camera on every opening: costs battery',
  settings_include: 'Settings lists the common controls first and the rest under More settings; entities named here join the list (guard-mode actions, entry and leaving delays)',
  settings_exclude: 'Controls of the camera to leave out of Settings',
  settings_groups: 'Tabs in Settings, in this order: drag to reorder, remove a group to hide it with its settings. While this list differs from the default, a group a later card version adds stays hidden; removing every group shows them all again',
  live_seconds: `A started live view ends after this (default ${LIVE_S} s); the ∞ button keeps it running until stopped`,
};

// The schema with the settings pickers limited to the chosen camera's selects, numbers and switches
const editorSchema = (hass, entity) => {
  const reg = (hass && hass.entities) || {};
  const me = reg[entity];
  const ids = me && me.device_id ? Object.values(reg).filter(e => e.device_id === me.device_id && ROW_DOMAINS.includes(e.entity_id.split('.')[0]))
    .map(e => e.entity_id).sort() : [];
  if (!ids.length) return { key: '', schema: [...EDITOR_SCHEMA, GROUPS_FIELD] };
  const pick = name => ({ name, selector: { entity: { multiple: true, include_entities: ids } } });
  return { key: ids.join(','), schema: [...EDITOR_SCHEMA, pick('settings_include'), pick('settings_exclude'), GROUPS_FIELD] };
};

class EufyCameraCardEditor extends HTMLElement {
  setConfig(config) { this._config = { ...config }; if (this._form) { this._schema(); this._form.data = this._data(); } }
  _schema() {
    const s = editorSchema(this._hass, this._config && this._config.entity);
    if (s.key !== this._schemaKey || !this._form.schema) { this._schemaKey = s.key; this._form.schema = s.schema; }
  }
  // auto_live: true is the YAML short form of 'timed'; no settings_groups shows every group in the card's order
  _data() {
    const d = { ...(this._config || {}) };
    if (d.auto_live === true) d.auto_live = 'timed';
    d.settings_groups = shownGroups(this._config);
    return d;
  }
  set hass(hass) {
    this._hass = hass;
    if (!this._form) {
      const root = this.shadowRoot || this.attachShadow({ mode: 'open' });
      this._form = document.createElement('ha-form');
      this._form.computeLabel = s => EDITOR_LABELS[s.name] || s.name;
      this._form.computeHelper = s => EDITOR_HELPERS[s.name];
      this._form.addEventListener('value-changed', (ev) => {
        ev.stopPropagation();
        const next = { ...this._config };
        Object.entries(ev.detail.value || {}).forEach(([k, v]) => {
          if (k === 'type') return;
          // Untouched, the key stays as written (a YAML [] too); the card's order with every group, or no group
          // left, drops it (every group shows)
          if (k === 'settings_groups') {
            const list = [...new Set([].concat(v || []).map(String))].filter(id => GROUP_IDS.includes(id));
            if (list.join() === shownGroups(this._config).join()) return;
            if (!list.length || list.join() === GROUP_IDS.join()) delete next[k]; else next[k] = list;
            return;
          }
          if (v === '' || v === undefined || v === null || (Array.isArray(v) && !v.length) || (k === 'layout' && v === 'auto') || (k === 'auto_live' && v === 'off') || (k === 'live_seconds' && v === LIVE_S)) delete next[k]; else next[k] = v;
        });
        this._config = next;
        this.dispatchEvent(new CustomEvent('config-changed', { detail: { config: next }, bubbles: true, composed: true }));
      });
      root.appendChild(this._form);
    }
    this._form.hass = hass;
    this._schema();
    this._form.data = this._data();
  }
}

if (!customElements.get('eufy-camera-card-editor')) customElements.define('eufy-camera-card-editor', EufyCameraCardEditor);
if (!customElements.get('eufy-camera-card')) customElements.define('eufy-camera-card', EufyCameraCard);
window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === 'eufy-camera-card')) {
  window.customCards.push({ type: 'eufy-camera-card', name: 'eufy camera',
    description: 'Still until started, timed or continuous live view, pan/tilt/zoom, presets and the camera\'s settings (staged)', preview: false });
}
console.info(`%c eufy-camera-card %c ${CARD_VERSION} `, 'background:#273a60;color:#fff', 'background:#ddd;color:#000');
