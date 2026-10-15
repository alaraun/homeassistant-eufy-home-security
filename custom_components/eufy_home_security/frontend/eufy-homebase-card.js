// eufy HomeBase card: a tile-style header with the security mode, a drawing of the HomeBase with its paired
// devices around it (what each does in a mode), and sections for the devices' actions per mode, arming groups,
// the station's settings and its status. Data comes from the integration's websocket commands
// (`eufy_home_security/homebase`, `.../subscribe`, `settings_layout`, `arming_groups/*`, the station-config writes);
// every write is staged under one ✕ / Set in the title line, and alarm-affecting writes (as the payload marks them)
// are listed for a confirm first. While the alarm sounds, Disarm and Stop alarm act at once.

const CARD_VERSION = '2026.10.15-5';

const DOMAIN = 'eufy_home_security';
const WS = {
  hb: `${DOMAIN}/homebase`, sub: `${DOMAIN}/homebase/subscribe`, table: `${DOMAIN}/homebase/mode_table/set`,
  restart: `${DOMAIN}/homebase/restart`, alarm: `${DOMAIN}/homebase/alarm`, layout: `${DOMAIN}/settings_layout`,
  mcreate: `${DOMAIN}/homebase/custom_mode/create`, mrename: `${DOMAIN}/homebase/custom_mode/rename`,
  mdelete: `${DOMAIN}/homebase/custom_mode/delete`, sched: `${DOMAIN}/homebase/schedule/set`,
  xcam: `${DOMAIN}/homebase/cross_camera/set`,
  glist: `${DOMAIN}/arming_groups/list`, gcreate: `${DOMAIN}/arming_groups/create`, gupdate: `${DOMAIN}/arming_groups/update`,
  gdelete: `${DOMAIN}/arming_groups/delete`, gact: `${DOMAIN}/arming_groups/activate`, glogs: `${DOMAIN}/arming_groups/logs`,
};
// A write is pending until the payload or the entity reports the sent value; Not applied after this
const PENDING_MS = 30000;
// Sound alarm length (s): the library's SOUND_ALARM_SECONDS
const SIREN_S = 30;
// Custom modes: name length and how many a station holds (the library's rules)
const MODE_NAME_MAX = 30;
const CUSTOM_MAX = 3;
// Schedule editor: time choices every SLOT_STEP minutes; a slot ends at DAY_END (midnight) at the latest (an end is exclusive)
const SLOT_STEP = 15;
const DAY_END = '24:00';
// Cross-camera tracking: the fewest cameras a choice may hold when the payload names none (the library's MIN_TRACKING_CAMERAS)
const XC_MIN = 2;
// Modes a schedule slot or its default can name besides the custom ones (the library's SCHEDULE_MODES)
const SCHED_BUILTIN = ['away', 'home', 'disarmed'];
// Ring capacity of the drawing: devices per page below / from this card width
const NARROW_W = 460;
const CAP_NARROW = 6;
const CAP_WIDE = 8;
// Drawing geometry (px): hub radius, row pitch of the device nodes, mode lens height
const HUB_R = 38;
const ROW_H = 92;
const LENS_H = 44;
// Looks only (names always come from the integration)
const FLAG_ICON = { record: 'record-rec', camera_siren: 'bullhorn', station_alarm: 'alarm-light', notification: 'bell',
  light_alarm: 'lightbulb-on', report_monitor_center: 'face-agent', motion_sensor_respond: 'access-point', privacy: 'eye-off',
  privacy_new: 'eye-off' };
const MODE_ICON = { away: 'shield-lock', home: 'shield-home', disarmed: 'shield-off', off: 'shield-off-outline',
  schedule: 'calendar-clock', geofencing: 'map-marker-radius', custom: 'security', triggered: 'bell-ring', pending: 'shield-outline' };
const KIND_ICON = { camera: 'cctv', sensor: 'motion-sensor', doorbell: 'doorbell-video', other: 'devices' };
// A refused write's message shows over the picture this long
const TOAST_MS = 6000;
// Dropdown lists: rows shown (at least DD_MIN_ROWS, all when fewer, up to DD_ROWS), a filter field above more than
// DD_FILTER options, the narrowest list (px) where the card has room, type-ahead reset (ms)
const DD_ROWS = 8;
const DD_MIN_ROWS = 5;
const DD_FILTER = 12;
const DD_MIN_W = 300;
const DD_TYPE_MS = 700;
const SCHED_COLORS = ['var(--teal-color, #009688)', 'var(--blue-color, #2196f3)', 'var(--purple-color, #926bc7)', 'var(--amber-color, #ffc107)',
  'var(--pink-color, #e91e63)', 'var(--brown-color, #795548)'];
const SEC_ICON = { alarm: 'alarm-light', notifications: 'bell', detection: 'cctv', system: 'cog', storage: 'harddisk', network: 'wifi' };
// MDI glyph paths (@mdi/js 7.4) for icons inside the drawing's SVG, where ha-icon cannot go
const SVG_ICONS = {
  'record-rec': 'M12.5,5A7.5,7.5 0 0,0 5,12.5A7.5,7.5 0 0,0 12.5,20A7.5,7.5 0 0,0 20,12.5A7.5,7.5 0 0,0 12.5,5M7,10H9A1,1 0 0,1 10,11V12C10,12.5 9.62,12.9 9.14,12.97L10.31,15H9.15L8,13V15H7M12,10H14V11H12V12H14V13H12V14H14V15H12A1,1 0 0,1 11,14V11A1,1 0 0,1 12,10M16,10H18V11H16V14H18V15H16A1,1 0 0,1 15,14V11A1,1 0 0,1 16,10M8,11V12H9V11',
  'bullhorn': 'M12,8H4A2,2 0 0,0 2,10V14A2,2 0 0,0 4,16H5V20A1,1 0 0,0 6,21H8A1,1 0 0,0 9,20V16H12L17,20V4L12,8M21.5,12C21.5,13.71 20.54,15.26 19,16V8C20.53,8.75 21.5,10.3 21.5,12Z',
  'alarm-light': 'M6,6.9L3.87,4.78L5.28,3.37L7.4,5.5L6,6.9M13,1V4H11V1H13M20.13,4.78L18,6.9L16.6,5.5L18.72,3.37L20.13,4.78M4.5,10.5V12.5H1.5V10.5H4.5M19.5,10.5H22.5V12.5H19.5V10.5M6,20H18A2,2 0 0,1 20,22H4A2,2 0 0,1 6,20M12,5A6,6 0 0,1 18,11V19H6V11A6,6 0 0,1 12,5Z',
  'bell': 'M21,19V20H3V19L5,17V11C5,7.9 7.03,5.17 10,4.29C10,4.19 10,4.1 10,4A2,2 0 0,1 12,2A2,2 0 0,1 14,4C14,4.1 14,4.19 14,4.29C16.97,5.17 19,7.9 19,11V17L21,19M14,21A2,2 0 0,1 12,23A2,2 0 0,1 10,21',
  'lightbulb-on': 'M12,6A6,6 0 0,1 18,12C18,14.22 16.79,16.16 15,17.2V19A1,1 0 0,1 14,20H10A1,1 0 0,1 9,19V17.2C7.21,16.16 6,14.22 6,12A6,6 0 0,1 12,6M14,21V22A1,1 0 0,1 13,23H11A1,1 0 0,1 10,22V21H14M20,11H23V13H20V11M1,11H4V13H1V11M13,1V4H11V1H13M4.92,3.5L7.05,5.64L5.63,7.05L3.5,4.93L4.92,3.5M16.95,5.63L19.07,3.5L20.5,4.93L18.37,7.05L16.95,5.63Z',
  'face-agent': 'M18.72,14.76C19.07,13.91 19.26,13 19.26,12C19.26,11.28 19.15,10.59 18.96,9.95C18.31,10.1 17.63,10.18 16.92,10.18C13.86,10.18 11.15,8.67 9.5,6.34C8.61,8.5 6.91,10.26 4.77,11.22C4.73,11.47 4.73,11.74 4.73,12A7.27,7.27 0 0,0 12,19.27C13.05,19.27 14.06,19.04 14.97,18.63C15.54,19.72 15.8,20.26 15.78,20.26C14.14,20.81 12.87,21.08 12,21.08C9.58,21.08 7.27,20.13 5.57,18.42C4.53,17.38 3.76,16.11 3.33,14.73H2V10.18H3.09C3.93,6.04 7.6,2.92 12,2.92C14.4,2.92 16.71,3.87 18.42,5.58C19.69,6.84 20.54,8.45 20.89,10.18H22V14.67H22V14.69L22,14.73H21.94L18.38,18L13.08,17.4V15.73H17.91L18.72,14.76M9.27,11.77C9.57,11.77 9.86,11.89 10.07,12.11C10.28,12.32 10.4,12.61 10.4,12.91C10.4,13.21 10.28,13.5 10.07,13.71C9.86,13.92 9.57,14.04 9.27,14.04C8.64,14.04 8.13,13.54 8.13,12.91C8.13,12.28 8.64,11.77 9.27,11.77M14.72,11.77C15.35,11.77 15.85,12.28 15.85,12.91C15.85,13.54 15.35,14.04 14.72,14.04C14.09,14.04 13.58,13.54 13.58,12.91A1.14,1.14 0 0,1 14.72,11.77Z',
  'access-point': 'M4.93,4.93C3.12,6.74 2,9.24 2,12C2,14.76 3.12,17.26 4.93,19.07L6.34,17.66C4.89,16.22 4,14.22 4,12C4,9.79 4.89,7.78 6.34,6.34L4.93,4.93M19.07,4.93L17.66,6.34C19.11,7.78 20,9.79 20,12C20,14.22 19.11,16.22 17.66,17.66L19.07,19.07C20.88,17.26 22,14.76 22,12C22,9.24 20.88,6.74 19.07,4.93M7.76,7.76C6.67,8.85 6,10.35 6,12C6,13.65 6.67,15.15 7.76,16.24L9.17,14.83C8.45,14.11 8,13.11 8,12C8,10.89 8.45,9.89 9.17,9.17L7.76,7.76M16.24,7.76L14.83,9.17C15.55,9.89 16,10.89 16,12C16,13.11 15.55,14.11 14.83,14.83L16.24,16.24C17.33,15.15 18,13.65 18,12C18,10.35 17.33,8.85 16.24,7.76M12,10A2,2 0 0,0 10,12A2,2 0 0,0 12,14A2,2 0 0,0 14,12A2,2 0 0,0 12,10Z',
  'eye-off': 'M11.83,9L15,12.16C15,12.11 15,12.05 15,12A3,3 0 0,0 12,9C11.94,9 11.89,9 11.83,9M7.53,9.8L9.08,11.35C9.03,11.56 9,11.77 9,12A3,3 0 0,0 12,15C12.22,15 12.44,14.97 12.65,14.92L14.2,16.47C13.53,16.8 12.79,17 12,17A5,5 0 0,1 7,12C7,11.21 7.2,10.47 7.53,9.8M2,4.27L4.28,6.55L4.73,7C3.08,8.3 1.78,10 1,12C2.73,16.39 7,19.5 12,19.5C13.55,19.5 15.03,19.2 16.38,18.66L16.81,19.08L19.73,22L21,20.73L3.27,3M12,7A5,5 0 0,1 17,12C17,12.64 16.87,13.26 16.64,13.82L19.57,16.75C21.07,15.5 22.27,13.86 23,12C21.27,7.61 17,4.5 12,4.5C10.6,4.5 9.26,4.75 8,5.2L10.17,7.35C10.74,7.13 11.35,7 12,7Z',
  'timer-outline': 'M12,20A7,7 0 0,1 5,13A7,7 0 0,1 12,6A7,7 0 0,1 19,13A7,7 0 0,1 12,20M19.03,7.39L20.45,5.97C20,5.46 19.55,5 19.04,4.56L17.62,6C16.07,4.74 14.12,4 12,4A9,9 0 0,0 3,13A9,9 0 0,0 12,22C17,22 21,17.97 21,13C21,10.88 20.26,8.93 19.03,7.39M11,14H13V8H11M15,1H9V3H15V1Z',
  'battery-20': 'M16,17H8V6H16M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z',
  'harddisk': 'M6,2H18A2,2 0 0,1 20,4V20A2,2 0 0,1 18,22H6A2,2 0 0,1 4,20V4A2,2 0 0,1 6,2M12,4A6,6 0 0,0 6,10C6,13.31 8.69,16 12.1,16L11.22,13.77C10.95,13.29 11.11,12.68 11.59,12.4L12.45,11.9C12.93,11.63 13.54,11.79 13.82,12.27L15.74,14.69C17.12,13.59 18,11.9 18,10A6,6 0 0,0 12,4M12,9A1,1 0 0,1 13,10A1,1 0 0,1 12,11A1,1 0 0,1 11,10A1,1 0 0,1 12,9M7,18A1,1 0 0,0 6,19A1,1 0 0,0 7,20A1,1 0 0,0 8,19A1,1 0 0,0 7,18M12.09,13.27L14.58,19.58L17.17,18.08L12.95,12.77L12.09,13.27Z',
  'shield-lock': 'M12,1L3,5V11C3,16.55 6.84,21.74 12,23C17.16,21.74 21,16.55 21,11V5L12,1M12,7C13.4,7 14.8,8.1 14.8,9.5V11C15.4,11 16,11.6 16,12.3V15.8C16,16.4 15.4,17 14.7,17H9.2C8.6,17 8,16.4 8,15.7V12.2C8,11.6 8.6,11 9.2,11V9.5C9.2,8.1 10.6,7 12,7M12,8.2C11.2,8.2 10.5,8.7 10.5,9.5V11H13.5V9.5C13.5,8.7 12.8,8.2 12,8.2Z',
  'shield-home': 'M11,13H13V16H16V11H18L12,6L6,11H8V16H11V13M12,1L21,5V11C21,16.55 17.16,21.74 12,23C6.84,21.74 3,16.55 3,11V5L12,1Z',
  'shield-off': 'M1,4.27L2.28,3L20.5,21.22L19.23,22.5L17,20.25C15.57,21.57 13.87,22.54 12,23C6.84,21.74 3,16.55 3,11V6.27L1,4.27M12,1L21,5V11C21,13.28 20.35,15.5 19.23,17.41L5.65,3.82L12,1Z',
  'security': 'M12,12H19C18.47,16.11 15.72,19.78 12,20.92V12H5V6.3L12,3.19M12,1L3,5V11C3,16.55 6.84,21.73 12,23C17.16,21.73 21,16.55 21,11V5L12,1Z',
  'calendar-clock': 'M15,13H16.5V15.82L18.94,17.23L18.19,18.53L15,16.69V13M19,8H5V19H9.67C9.24,18.09 9,17.07 9,16A7,7 0 0,1 16,9C17.07,9 18.09,9.24 19,9.67V8M5,21C3.89,21 3,20.1 3,19V5C3,3.89 3.89,3 5,3H6V1H8V3H16V1H18V3H19A2,2 0 0,1 21,5V11.1C22.24,12.36 23,14.09 23,16A7,7 0 0,1 16,23C14.09,23 12.36,22.24 11.1,21H5M16,11.15A4.85,4.85 0 0,0 11.15,16C11.15,18.68 13.32,20.85 16,20.85A4.85,4.85 0 0,0 20.85,16C20.85,13.32 18.68,11.15 16,11.15Z',
  'bell-ring': 'M21,19V20H3V19L5,17V11C5,7.9 7.03,5.17 10,4.29C10,4.19 10,4.1 10,4A2,2 0 0,1 12,2A2,2 0 0,1 14,4C14,4.1 14,4.19 14,4.29C16.97,5.17 19,7.9 19,11V17L21,19M14,21A2,2 0 0,1 12,23A2,2 0 0,1 10,21M19.75,3.19L18.33,4.61C20.04,6.3 21,8.6 21,11H23C23,8.07 21.84,5.25 19.75,3.19M1,11H3C3,8.6 3.96,6.3 5.67,4.61L4.25,3.19C2.16,5.25 1,8.07 1,11Z',
  'shield-outline': 'M21,11C21,16.55 17.16,21.74 12,23C6.84,21.74 3,16.55 3,11V5L12,1L21,5V11M12,21C15.75,20 19,15.54 19,11.22V6.3L12,3.18L5,6.3V11.22C5,15.54 8.25,20 12,21Z',
  'map-marker-radius': 'M12,2C15.31,2 18,4.66 18,7.95C18,12.41 12,19 12,19C12,19 6,12.41 6,7.95C6,4.66 8.69,2 12,2M12,6A2,2 0 0,0 10,8A2,2 0 0,0 12,10A2,2 0 0,0 14,8A2,2 0 0,0 12,6M20,19C20,21.21 16.42,23 12,23C7.58,23 4,21.21 4,19C4,17.71 5.22,16.56 7.11,15.83L7.75,16.74C6.67,17.19 6,17.81 6,18.5C6,19.88 8.69,21 12,21C15.31,21 18,19.88 18,18.5C18,17.81 17.33,17.19 16.25,16.74L16.89,15.83C18.78,16.56 20,17.71 20,19Z',
  'alert-circle': 'M13,13H11V7H13M13,17H11V15H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z',
  'shield-off-outline': 'M1,4.27L3,6.27V11C3,16.55 6.84,21.74 12,23C13.87,22.54 15.57,21.56 16.97,20.24L19.23,22.5L20.5,21.22L2.28,3L1,4.27M12,21C8.25,20 5,15.54 5,11.22V8.27L15.59,18.86C14.53,19.89 13.3,20.65 12,21M21,5V11C21,13.28 20.35,15.5 19.23,17.4L17.77,15.95C18.54,14.5 19,12.84 19,11.22V6.3L12,3.18L7.16,5.34L5.65,3.82L12,1L21,5Z',
  'help-circle-outline': 'M11,18H13V16H11V18M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2M12,20C7.59,20 4,16.41 4,12C4,7.59 7.59,4 12,4C16.41,4 20,7.59 20,12C20,16.41 16.41,20 12,20M12,6A4,4 0 0,0 8,10H10A2,2 0 0,1 12,8A2,2 0 0,1 14,10C14,12 11,11.75 11,15H13C13,12.75 16,12.5 16,10A4,4 0 0,0 12,6Z',
};

const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const icon = (n, cls = '') => `<ha-icon${cls ? ` class="${cls}"` : ''} icon="mdi:${n}"></ha-icon>`;
const svgIcon = (n, x, y, s, cls = '') => {
  const d = SVG_ICONS[n] || SVG_ICONS['alert-circle'];
  return `<path class="gi${cls ? ` ${cls}` : ''}" d="${d}" transform="translate(${(x - s / 2).toFixed(1)} ${(y - s / 2).toFixed(1)}) scale(${(s / 24).toFixed(4)})"/>`;
};
const NA = v => v == null || v === 'unavailable' || v === 'unknown' || v === '';
const human = s => { const t = String(s || '').replace(/_/g, ' '); return t.charAt(0).toUpperCase() + t.slice(1); };
const cssEsc = s => (window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/"/g, '\\"'));

// Clock time in the HA profile's format: time_format '12' | '24' | 'language' (its default) | 'system' (the browser's)
const clockTime = (hass, d) => {
  const loc = (hass && hass.locale) || {};
  const lang = loc.time_format === 'system' ? undefined : (loc.language || (hass && hass.language) || undefined);
  const o = { hour: '2-digit', minute: '2-digit' };
  if (loc.time_format === '12') o.hour12 = true;
  if (loc.time_format === '24') o.hourCycle = 'h23';
  return d.toLocaleTimeString(lang, o);
};
const hhmm = (hass, s) => {
  const [h, m] = String(s || '0:0').split(':').map(Number), t = clockTime(hass, new Date(2000, 0, 1, h || 0, m || 0));
  // a slot end at midnight: 24:00 where the clock shows 00:00
  return h === 24 ? t.replace(/^0?0(?=\D)/, '24') : t;
};
const minutes = (s, end) => { const [h, m] = String(s || '0:0').split(':').map(Number); const v = (h || 0) * 60 + (m || 0); return end && v === 0 ? 1440 : v; };
const hm = v => `${String(Math.floor(v / 60)).padStart(2, '0')}:${String(v % 60).padStart(2, '0')}`;
// Short weekday name (0 = Sunday) in the HA language
const dayName = (hass, wd) => new Date(2024, 0, 7 + wd).toLocaleDateString(hass && hass.locale && hass.locale.language, { weekday: 'short' });
// A schedule's identity: its slots in any order and the default
const schedKey = v => `${(v.slots || []).map(x => `${x.weekday}|${x.start}|${x.end}|${x.mode}`).sort().join(',')}#${v.default || ''}`;
const dur = s => (s % 60 ? `${s} s` : `${s / 60} min`);

let MEASURE = null;
const textW = (t, font) => { if (!MEASURE) MEASURE = document.createElement('canvas').getContext('2d'); MEASURE.font = font; return MEASURE.measureText(String(t)).width; };

class EufyHomebaseCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._hb = null;        // homebase payload
    this._lay = null;       // settings layout sections, null until loaded
    this._groups = null;    // { groups, candidates } | { err } | null
    this._logs = {};        // group id -> logs
    this._gres = {};        // group id -> last activation results
    this._err = null;       // load error { code, message }
    this._open = null;      // open section: devices | groups | settings | status
    this._dev = null;       // device shown in Devices
    this._page = 0;         // drawing page
    this._preview = null;   // mode the drawing / matrix shows (or 'schedule')
    this._stab = null;      // open Settings tab
    this._gview = null;     // group being edited ('new' for a new one)
    this._dsel = {};        // device id -> { mode, which } chosen delay cell
    this._dd = null;        // open dropdown key
    this._ddq = '';         // its filter text
    this._ddt = { s: '', t: 0 }; // type-ahead buffer
    this._menu = false;
    this._confirm = false;
    this._medit = false;    // custom modes editor over the picture
    this._mnew = false;     // its new-mode field shown
    this._sform = null;     // schedule slot form { idx, days, start, end, mode }
    this._inuse = {};       // mode id -> { reason, rev }: a delete the HomeBase refused (mode_in_use)
    this._note = null;      // header note: 'changed' after a config_changed refusal
    this._stg = new Map();  // staged writes, key -> entry
    this._pend = new Map(); // sent writes, key -> { entry, status: pending | failed, t, msg }
    this._w = 0;
    this._fk = null;        // focus target after the next render
  }

  static getConfigElement() { return document.createElement('eufy-homebase-card-editor'); }
  static getStubConfig(hass) { const s = stations(hass); return { type: 'custom:eufy-homebase-card', device_id: s[0] || '' }; }

  setConfig(config) {
    this._config = { ...(config || {}) };
    this._hb = null; this._lay = null; this._groups = null; this._err = null;
    this._stg.clear(); this._pend.clear(); this._open = null; this._dev = null; this._page = 0; this._preview = null;
    this._unsubscribe();
    if (this._hass) this._load();
    this._render();
  }
  getCardSize() { return 6; }
  getGridOptions() { return { columns: 12, min_columns: 6 }; }

  connectedCallback() {
    if (!this._ro) {
      this._ro = new ResizeObserver((es) => { const w = Math.round(es[0].contentRect.width); if (w && w !== this._w) { this._w = w; this._render(); } });
      this._io = new IntersectionObserver((es) => { this._visible = es[0].isIntersecting; const r = this.shadowRoot.querySelector('.cc'); if (r) r.classList.toggle('paused', !this._visible); });
    }
    this._ro.observe(this); this._io.observe(this);
    this._outside = (e) => {
      if (!this._menu && !this._dd) return;
      const p = e.composedPath();
      if (this._menu && !p.includes(this.shadowRoot.querySelector('.bwrap'))) { this._menu = false; this._render(); }
      else if (this._dd && !p.some(n => n.classList && n.classList.contains('ddw'))) { this._dd = null; this._ddq = ''; this._render(); }
    };
    document.addEventListener('pointerdown', this._outside, true);
    if (this._hass && this._devId() && this._hb && !this._unsub) this._subscribe();
  }
  disconnectedCallback() {
    if (this._ro) { this._ro.disconnect(); this._io.disconnect(); }
    document.removeEventListener('pointerdown', this._outside, true);
    clearTimeout(this._pt);
    this._unsubscribe();
  }

  set hass(h) {
    const prev = this._hass;
    this._hass = h;
    if (!prev) { this._render(); this._load(); return; }
    if (this._hb && prev.entities !== h.entities && this._entKey() !== this._ek) this._loadLayout();
    this._settle();
    if (this._stateKey() !== this._sk || this._pend.size) this._render();
  }
  get hass() { return this._hass; }

  _devId() {
    const c = this._config || {};
    if (c.device_id) return c.device_id;
    const e = c.entity && this._hass && this._hass.entities && this._hass.entities[c.entity];
    return e ? e.device_id : null;
  }

  // ---------- loading ----------
  async _load() {
    const id = this._devId();
    if (!id) { this._render(); return; }
    const tok = this._tok = {};
    try {
      const hb = await this._hass.callWS({ type: WS.hb, device_id: id });
      if (tok !== this._tok) return;
      this._hb = hb; this._err = null;
    } catch (e) {
      if (tok !== this._tok) return;
      this._err = { code: e && e.code, message: (e && e.message) || String(e) }; this._render(); return;
    }
    this._subscribe();
    this._loadLayout();
    if (this._shows('arming_groups')) this._loadGroups();
    this._render();
  }
  async _reload() {
    try { this._adopt(await this._hass.callWS({ type: WS.hb, device_id: this._devId() })); } catch (e) { /* the last payload stays */ }
  }
  async _subscribe() {
    this._unsubscribe();
    const conn = this._hass && this._hass.connection;
    if (!conn || !conn.subscribeMessage) return;
    const tok = this._stok = {};
    try {
      const un = await conn.subscribeMessage((ev) => { const p = ev && ev.modes ? ev : ev && ev.payload; if (p) this._adopt(p); },
        { type: WS.sub, device_id: this._devId() });
      if (tok !== this._stok || !this.isConnected) { un(); return; }
      this._unsub = un;
    } catch (e) { /* the card still works from reads */ }
  }
  _unsubscribe() { this._stok = null; if (this._unsub) { const u = this._unsub; this._unsub = null; try { u(); } catch (e) { /* closed */ } } }
  _entKey() {
    const id = this._hb && this._hb.device_id; const ents = (this._hass && this._hass.entities) || {};
    return Object.keys(ents).filter(k => ents[k].device_id === id).sort().join(',');
  }
  async _loadLayout() {
    this._ek = this._entKey();
    try {
      const r = await this._hass.callWS({ type: WS.layout, device_id: this._hb.device_id });
      this._lay = (r && r.sections) || [];
    } catch (e) { if (!this._lay) this._lay = []; }
    this._render();
  }
  async _loadGroups() {
    try {
      const r = await this._hass.callWS({ type: WS.glist, device_id: this._hb.device_id });
      this._groups = { groups: r.groups || [], candidates: r.candidates || [] };
    } catch (e) {
      this._groups = { err: (e && e.code) || 'error', groups: [], candidates: [] };
    }
    this._render();
  }
  async _loadLogs(gid) {
    if (gid === 'new' || this._logs[gid]) return;
    this._logs[gid] = null;
    try { const r = await this._hass.callWS({ type: WS.glogs, device_id: this._hb.device_id, group_id: gid }); this._logs[gid] = r.logs || []; } catch (e) { this._logs[gid] = []; }
    this._render();
  }
  _adopt(p) {
    if (!p || !p.modes) return;
    this._hb = p;
    if (this._dev && !p.devices.some(d => d.device_id === this._dev)) this._dev = null;
    this._settle();
    this._render();
  }

  // ---------- payload helpers ----------
  _st(id) { return id && this._hass && this._hass.states[id]; }
  _mode(id) { return this._hb.modes.find(m => m.id === id); }
  _mname(id) { const m = this._mode(id); return m ? m.name : id ? human(id) : 'Mode not reported'; }
  _sup(f) { return (this._hb.support || {})[f] || { grade: 'unknown' }; }
  _shows(f) { return this._sup(f).grade !== 'unknown'; }
  _affects(f) { return this._sup(f).alarm_affecting !== false; }
  // The integration can write the feature today (support.<f>.available)
  _can(f) { return this._sup(f).available !== false; }
  _kindIcon(k) { return KIND_ICON[k] || 'cctv'; }
  // The security-mode select's option for a mode: the payload's modes[].option
  _modeOption(id) { const m = this._mode(id); return (m && m.option) || this._mname(id); }
  _say(text) {
    if (!text) return;
    this._toast = { text, until: Date.now() + TOAST_MS };
    clearTimeout(this._tt); this._tt = setTimeout(() => { this._toast = null; this._render(); }, TOAST_MS + 20);
  }
  _triggered() { const p = this._st(this._hb.alarm_panel); return !!((this._hb.alarm && this._hb.alarm.triggered) || (p && p.state === 'triggered')); }
  _fam(id, depth = 0) {
    const m = this._mode(id);
    if ((id === 'schedule' || id === 'geofencing') && depth < 2) { const e = this._hb.effective_mode; return e && e !== id ? this._fam(e, depth + 1) : 'home'; }
    if (m && m.custom) return 'custom';
    if (id === 'away' || id === 'home' || id === 'disarmed') return id;
    if (id === 'off') return 'disarmed';
    if (/^(custom|code_)/.test(String(id))) return 'custom';
    return 'pending';
  }
  _micon(id) { return MODE_ICON[id] || MODE_ICON[this._fam(id)] || 'shield-outline'; }
  _tmodes() { const t = this._hb.mode_tables || {}; return this._hb.modes.filter(m => t[m.id]); }
  _flagsOf(d) {
    const kind = this._hb.flags[d.kind] || [];
    const ids = d.flags ? d.flags.map(f => (typeof f === 'string' ? f : f.id)) : kind.map(f => f.id);
    return ids.map(id => kind.find(f => f.id === id) || (d.flags || []).find(f => f && f.id === id) || this._flagDef(id) || { id, label: human(id) });
  }
  _flagDef(id) { for (const l of Object.values(this._hb.flags || {})) { const f = (l || []).find(x => x.id === id); if (f) return f; } return null; }
  // Alarm-affecting as the payload's flag lists say; a flag no list describes is confirmed
  _alarmFlag(f) { const d = f.alarm_affecting !== undefined ? f : this._flagDef(f.id); return !d || d.alarm_affecting !== false; }
  _row(mode, dev) { const t = (this._hb.mode_tables || {})[mode]; return t && t.devices ? t.devices[dev] : undefined; }
  _liveFlag(mode, dev, flag) { const r = this._row(mode, dev); return r && r.flags ? r.flags[flag] : undefined; }
  _liveDelay(mode, dev, which) { const r = this._row(mode, dev); return r && typeof r[which] === 'number' ? r[which] : undefined; }
  _liveModeDelay(mode, which) {
    const t = (this._hb.mode_tables || {})[mode]; if (!t || !t.devices) return undefined;
    const v = Object.values(t.devices).map(r => r && r[which]).filter(x => typeof x === 'number');
    return v.length ? Math.max(...v) : undefined;
  }
  _val(key, live) { const s = this._stg.get(key); if (s) return s.value; const p = this._pend.get(key); if (p && p.status === 'pending') return p.entry.value; return live; }
  _flag(mode, dev, flag) { return this._val(`t|${mode}|${dev}|${flag}`, this._liveFlag(mode, dev, flag)); }
  _modeDelay(mode, which) { return this._val(`dv|${mode}|${which}`, this._liveModeDelay(mode, which)); }
  _devDelayOn(mode, dev, which) { const l = this._liveDelay(mode, dev, which); return this._val(`dd|${mode}|${which}|${dev}`, l === undefined ? undefined : l > 0); }
  _devDelay(mode, dev, which) {
    const on = this._devDelayOn(mode, dev, which); if (on === undefined) return undefined; if (!on) return 0;
    const touched = this._stg.has(`dv|${mode}|${which}`) || this._stg.has(`dd|${mode}|${which}|${dev}`) || this._pend.has(`dv|${mode}|${which}`) || this._pend.has(`dd|${mode}|${which}|${dev}`);
    const l = this._liveDelay(mode, dev, which);
    return touched || !l ? (this._modeDelay(mode, which) || 0) : l;
  }
  // Delay choices (s) from the payload; without them the delays show but cannot be changed
  _steps() { const c = this._hb.delay_choices; return Array.isArray(c) ? c : []; }
  _status(key) { if (this._stg.has(key)) return 'staged'; const p = this._pend.get(key); return p ? p.status : ''; }
  _online(d) { return d.online !== false && this._hb.connected !== false; }
  _devs() { return this._hb.devices || []; }
  _previewMode() {
    const ids = this._tmodes().map(m => m.id);
    if (this._preview === 'schedule' && this._schedOk()) return 'schedule';
    if (this._preview && ids.includes(this._preview)) return this._preview;
    const e = this._hb.effective_mode, g = this._hb.guard_mode;
    return ids.includes(e) ? e : ids.includes(g) ? g : ids[0];
  }
  _schedOk() { return !!this._hb.schedule && this._shows('schedule'); }
  // Modes a schedule slot or its default can name, in the payload's order
  _schedModes() { return this._hb.modes.filter(m => m.custom || SCHED_BUILTIN.includes(m.id)); }
  _liveSched() {
    const S = this._hb.schedule;
    return { slots: S && S.slots ? S.slots.map(x => ({ weekday: x.weekday, start: x.start, end: x.end, mode: x.mode })) : [], default: (S && S.default) || null };
  }
  _schedVal() { const s = this._stg.get('sched'); if (s) return s.value; const p = this._pend.get('sched'); if (p && p.status === 'pending') return p.entry.value; return this._liveSched(); }
  // One palette colour per schedule mode (the armed modes share one HA state colour); Disarmed grey
  _scol(id) {
    if (id === 'disarmed' || id === 'off') return 'var(--m-disarmed)';
    const ids = this._schedModes().map(m => m.id).filter(x => x !== 'disarmed'), i = ids.indexOf(id);
    return SCHED_COLORS[(i < 0 ? ids.length : i) % SCHED_COLORS.length];
  }
  // The slot (index other than skip) that shares a minute with c on its weekday; an end is exclusive
  _overlap(slots, c, skip = -1) { return slots.find((x, i) => i !== skip && x.weekday === c.weekday && minutes(x.start) < minutes(c.end, true) && minutes(c.start) < minutes(x.end, true)) || null; }
  _slotText(x) { return `${dayName(this._hass, x.weekday)} ${hhmm(this._hass, x.start)}–${hhmm(this._hass, x.end)} · ${this._mname(x.mode)}`; }
  _xcLive() { const c = this._hb.cross_camera || {}; return { cams: c.cameras === undefined ? null : c.cameras, groups: c.groups || [] }; }
  // Devices cross-camera tracking can follow: the payload's eligible list, in its order
  _xcCams() { const devs = this._devs(); return ((this._hb.cross_camera || {}).eligible || []).map(id => devs.find(d => d.device_id === id)).filter(Boolean); }
  _xcMin() { const m = (this._hb.cross_camera || {}).min_cameras; return typeof m === 'number' ? m : XC_MIN; }
  // A camera choice (null = every eligible one) that holds fewer than the minimum
  _xcShort(v) { const ids = this._xcCams().map(d => d.device_id); return (v === null ? ids.length : v.filter(id => ids.includes(id)).length) < this._xcMin(); }
  // Why Set is refused ('' = it is not): a staged camera choice below the minimum
  _setBlock() { const e = this._stg.get('xc|cams'); return e && this._xcShort(e.value) ? `Choose at least ${this._xcMin()} cameras to track` : ''; }
  _stateKey() {
    if (!this._hb || !this._hass) return '';
    const S = this._hass.states, ids = [this._hb.alarm_panel, this._hb.security_mode, this._hb.status && this._hb.status.update];
    (this._lay || []).forEach(s => ids.push(...s.entity_ids));
    return ids.map(i => { const s = i && S[i]; return s ? `${s.state}|${s.attributes.options ? s.attributes.options.length : ''}` : '-'; }).join(',');
  }

  // ---------- staging ----------
  _stage(key, entry, live) {
    if (entry.value !== undefined && live !== undefined && String(entry.value) === String(live)) this._stg.delete(key);
    else this._stg.set(key, entry);
    this._pend.delete(key);
    this._confirm = false; this._note = null; this._toast = null;
  }
  _toggleStage(key, entry) { if (this._stg.has(key)) this._stg.delete(key); else { this._stg.set(key, entry); this._pend.delete(key); } this._confirm = false; this._note = null; }
  _stageFlag(mode, dev, flag) {
    const f = this._flagsOf(this._dev2(dev)).find(x => x.id === flag) || { id: flag };
    this._stage(`t|${mode}|${dev}|${flag}`, { kind: 'flag', mode, dev, flag, value: !this._flag(mode, dev, flag), alarm: this._alarmFlag(f) && this._affects('mode_tables') },
      this._liveFlag(mode, dev, flag));
  }
  _stageDelayValue(mode, which, v) {
    this._stage(`dv|${mode}|${which}`, { kind: 'dval', mode, which, value: v, alarm: this._affects('mode_delays') }, this._liveModeDelay(mode, which));
  }
  _stageDelayDev(mode, dev, which, on) {
    const l = this._liveDelay(mode, dev, which);
    this._stage(`dd|${mode}|${which}|${dev}`, { kind: 'ddev', mode, which, dev, value: on, alarm: this._affects('mode_delays') }, l === undefined ? undefined : l > 0);
    const first = this._steps().find(x => x > 0);
    if (on && !this._modeDelay(mode, which) && first) this._stageDelayValue(mode, which, first);
  }
  _stageEnt(id, value, sec) {
    const s = this._st(id); if (!s) return;
    // alarm-affecting as the layout says: the section, or the entity among its alarm_affecting_entity_ids
    const alarm = !!sec && (!!sec.alarm_affecting || (sec.alarm_affecting_entity_ids || []).includes(id));
    this._stage(`e|${id}`, { kind: 'ent', id, value: String(value), alarm, label: this._ename(id) }, s.state);
  }
  // A custom mode's name (id null: the new mode); an empty or unchanged name stages nothing
  _stageModeName(id, name) {
    const nm = String(name || '').trim(), alarm = this._affects('custom_modes');
    if (id === null) { if (nm) this._stage('mc|new', { kind: 'mnew', name: nm, alarm }); else { this._stg.delete('mc|new'); this._mnew = false; } return; }
    const m = this._mode(id); if (!m) return;
    if (!nm) { this._stg.delete(`mc|ren|${id}`); return; }
    this._stage(`mc|ren|${id}`, { kind: 'mren', mode: id, name: nm, value: nm, alarm }, m.name);
  }
  _stageModeDel(id) {
    this._stg.delete(`mc|ren|${id}`);
    this._toggleStage(`mc|del|${id}`, { kind: 'mdel', mode: id, alarm: this._affects('custom_modes') });
  }
  // The whole schedule (slots and default) as one staged write
  _stageSched(v) {
    if (schedKey(v) === schedKey(this._liveSched())) { this._stg.delete('sched'); this._pend.delete('sched'); this._confirm = false; return; }
    this._stage('sched', { kind: 'sched', value: v, alarm: this._affects('schedule') });
  }
  _stageX(key, kind, value, live, feat) {
    if (JSON.stringify(value) === JSON.stringify(live)) { this._stg.delete(key); this._pend.delete(key); this._confirm = false; return; }
    this._stage(key, { kind, value, alarm: this._affects(feat) });
  }
  _sf() {
    if (!this._sform) { const m = this._schedModes().find(x => x.id !== 'disarmed') || this._schedModes()[0] || { id: 'home' }; this._sform = { idx: null, days: [1], start: '08:00', end: '17:00', mode: m.id }; }
    return this._sform;
  }
  _loadSlot(i) {
    const v = this._schedVal(), x = i === null ? null : v.slots[i];
    if (!x) { this._sform = null; this._sf(); return; }
    this._sform = { idx: i, days: [x.weekday], start: x.start, end: x.end, mode: x.mode };
  }
  // Why the form's slot cannot be added or saved ('' = it can)
  _sformCheck(v, f) {
    if (!f.days.length) return 'Choose a day';
    if (minutes(f.start) >= minutes(f.end, true)) return 'The slot must start before it ends';
    for (const wd of f.days) {
      const o = this._overlap(v.slots, { weekday: wd, start: f.start, end: f.end }, f.idx === null ? -1 : f.idx);
      if (o) return `Overlaps ${this._slotText(o)}`;
    }
    return '';
  }
  _slotAct(a) {
    const v = this._schedVal(), f = this._sf();
    const mk = () => f.days.map(wd => ({ weekday: wd, start: f.start, end: f.end, mode: f.mode }));
    if (a === 'new') { this._sform = null; this._sf(); return; }
    if (a === 'del' && f.idx !== null) { this._stageSched({ ...v, slots: v.slots.filter((_, i) => i !== f.idx) }); this._sform = { ...f, idx: null }; return; }
    if ((a === 'add' || a === 'save') && !this._sformCheck(v, f)) {
      const rest = a === 'save' && f.idx !== null ? v.slots.filter((_, i) => i !== f.idx) : v.slots;
      this._stageSched({ ...v, slots: [...rest, ...mk()] });
      this._sform = { ...f, idx: null };
    }
  }
  _dev2(id) { return this._devs().find(d => d.device_id === id) || { kind: 'camera', name: id }; }
  _alarmEntries() { return [...this._stg.entries()].filter(([, e]) => e.alarm); }
  _effect([, e]) {
    const mn = e.mode ? this._mname(e.mode) : '';
    if (e.kind === 'mode') return [this._micon(e.value), e.value === 'disarmed' ? 'Disarms the HomeBase now' : `Sets the HomeBase to ${this._mname(e.value)} now`];
    if (e.kind === 'flag') {
      const d = this._dev2(e.dev), f = this._flagsOf(d).find(x => x.id === e.flag) || { label: human(e.flag) };
      return [FLAG_ICON[e.flag] || 'alert-circle', `${mn} · ${d.name}: ${f.label} ${e.value ? 'on' : 'off'}`];
    }
    if (e.kind === 'dval') { const l = this._liveModeDelay(e.mode, e.which); return ['timer-outline', `${mn} · ${e.which === 'entry' ? 'Entry delay' : 'Leaving delay'} ${l === undefined ? '' : `${l} s → `}${e.value} s${e.value === 0 ? ' (off for every device)' : ''}`]; }
    if (e.kind === 'ddev') { const d = this._dev2(e.dev); return ['timer-outline', `${mn} · ${d.name}: ${e.which === 'entry' ? 'entry delay' : 'leaving delay'} ${e.value ? 'on' : 'off'}`]; }
    if (e.kind === 'gact') { const g = this._group(e.gid); return ['account-group', `Activates ${g ? g.name : 'a group'}: ${(g ? g.members : []).map(m => `${m.name || 'a device'} → ${m.label || this._mname(m.mode)}`).join(', ')}`]; }
    if (e.kind === 'restart') return ['restart', 'Restarts the HomeBase: no alarm until it is back'];
    if (e.kind === 'siren') return e.action === 'stop' ? ['bell-off', 'Stops the HomeBase alarm'] : ['bullhorn', `Sounds the HomeBase alarm for ${SIREN_S} s`];
    if (e.kind === 'mnew') return ['plus', `Adds the mode ${e.name}`];
    if (e.kind === 'mren') return ['pencil', `Renames ${this._mname(e.mode)} to ${e.name}`];
    if (e.kind === 'mdel') return ['delete', `Deletes the mode ${this._mname(e.mode)}`];
    if (e.kind === 'sched') {
      const l = this._liveSched();
      return ['calendar-clock', `Schedule: ${l.slots.length} → ${e.value.slots.length} slots${e.value.default && e.value.default !== l.default ? `; outside the slots ${this._mname(e.value.default)}` : ''}`];
    }
    if (e.kind === 'xcams') return ['cctv', `Cross-camera tracking: ${e.value === null ? 'all cameras' : e.value.map(id => this._dev2(id).name).join(', ')}`];
    if (e.kind === 'xgroups') return ['cctv', `Tracking groups: ${e.value.filter(g => g.length).map(g => g.map(id => (id ? this._dev2(id).name : 'a camera')).join(' + ')).join('; ') || 'none'}`];
    if (e.kind === 'ent') { const s = this._st(e.id); return ['volume-high', `${e.label}: ${s ? this._fmt(e.id, s.state) : ''} → ${this._fmt(e.id, e.value)}`]; }
    return ['alert-circle', human(e.kind)];
  }
  _group(id) { return this._groups && this._groups.groups ? this._groups.groups.find(g => g.group_id === id) : null; }
  _cancel() { this._stg.clear(); for (const [k, p] of [...this._pend]) if (p.status === 'failed') this._pend.delete(k); this._confirm = false; this._note = null; this._mnew = false; this._sform = null; this._fk = 'menu'; this._render(); }

  // ---------- Set ----------
  _set(confirmed) {
    if (!this._stg.size || this._setBlock()) return;
    if (!confirmed && this._alarmEntries().length) { this._confirm = true; this._fk = 'cf-set'; this._render(); return; }
    this._confirm = false; this._note = null; this._mnew = false;
    const entries = [...this._stg.entries()];
    this._stg.clear();
    const t = Date.now();
    for (const [k, e] of entries) this._pend.set(k, { entry: e, status: 'pending', t });
    this._fk = 'menu';
    this._render();
    this._send(entries);
  }
  async _send(entries) {
    const H = this._hass, dev = this._hb.device_id, sent = new Set();
    const fail = (keys, err) => { for (const k of keys) { const p = this._pend.get(k); if (p) { p.status = 'failed'; p.msg = err && err.message; } } this._say(err && err.message); this._render(); };
    // config_changed: reload, stage again the edits not sent yet that still apply, stop
    const changed = async () => { await this._reload(); this._restage(entries.filter(([k]) => !sent.has(k))); this._note = 'changed'; this._render(); };
    // one station-config write answered by the fresh payload; false = stopped on config_changed
    const cfg = async (list, req) => {
      try {
        const res = await H.callWS({ ...req, device_id: dev, expect_revision: this._hb.revision });
        list.forEach(([k]) => sent.add(k));
        if (res && res.modes) this._adopt(res); else await this._reload();
      } catch (err) {
        if (err && err.code === 'config_changed') { await changed(); return false; }
        list.forEach(([k]) => sent.add(k));
        if (err && err.code === 'mode_in_use') list.forEach(([, e]) => { if (e.mode) this._inuse[e.mode] = { reason: err.message, rev: this._hb.revision }; });
        fail(list.map(([k]) => k), err);
      }
      return true;
    };
    // 1. one mode-table write per edited mode, in the payload's mode order
    const tab = entries.filter(([, e]) => ['flag', 'dval', 'ddev'].includes(e.kind));
    const order = this._hb.modes.map(m => m.id);
    const modes = [...new Set(tab.map(([, e]) => e.mode))].sort((a, b) => order.indexOf(a) - order.indexOf(b));
    for (let i = 0; i < modes.length; i++) {
      const mode = modes[i], mine = tab.filter(([, e]) => e.mode === mode);
      const req = { type: WS.table, device_id: dev, mode, actions: {}, expect_revision: this._hb.revision };
      for (const [, e] of mine) {
        if (e.kind === 'flag') (req.actions[e.dev] = req.actions[e.dev] || {})[e.flag] = e.value;
        else {
          const s = e.which === 'entry' ? 'entry_s' : 'exit_s', ds = e.which === 'entry' ? 'entry_devices' : 'exit_devices';
          req[s] = this._modeDelay(mode, e.which) || 0;
          if (e.kind === 'ddev') (req[ds] = req[ds] || {})[e.dev] = e.value;
        }
      }
      try {
        const res = await H.callWS(req);
        mine.forEach(([k]) => sent.add(k));
        if (res && res.modes) this._adopt(res); else await this._reload();
      } catch (err) {
        if (err && err.code === 'config_changed') { await changed(); return; }
        mine.forEach(([k]) => sent.add(k));
        fail(mine.map(([k]) => k), err);
      }
    }
    // 2. settings, in the layout's order
    const ents = entries.filter(([, e]) => e.kind === 'ent');
    for (const [k, e] of ents) {
      const dom = e.id.split('.')[0]; sent.add(k);
      try {
        if (dom === 'switch') await H.callService('switch', e.value === 'on' ? 'turn_on' : 'turn_off', { entity_id: e.id });
        else if (dom === 'number') await H.callService('number', 'set_value', { entity_id: e.id, value: Number(e.value) });
        else if (dom === 'select') await H.callService('select', 'select_option', { entity_id: e.id, option: e.value });
      } catch (err) { fail([k], err); }
    }
    // 3. custom modes added and renamed, the schedule, cross-camera picks and groups (one write), modes deleted
    const of = kind => entries.filter(([, e]) => e.kind === kind);
    for (const x of of('mnew')) if (!await cfg([x], { type: WS.mcreate, name: x[1].name })) return;
    for (const x of of('mren')) if (!await cfg([x], { type: WS.mrename, mode: x[1].mode, name: x[1].name })) return;
    for (const x of of('sched')) {
      const v = x[1].value;
      if (!await cfg([x], { type: WS.sched, slots: v.slots, ...(v.default ? { default: v.default } : {}) })) return;
    }
    const xc = entries.filter(([, e]) => e.kind === 'xcams' || e.kind === 'xgroups');
    if (xc.length) {
      const req = { type: WS.xcam };
      for (const [, e] of xc) { if (e.kind === 'xcams') req.cameras = e.value; else req.groups = e.value.map(g => g.filter(Boolean)).filter(g => g.length); }
      if (!await cfg(xc, req)) return;
    }
    for (const x of of('mdel')) if (!await cfg([x], { type: WS.mdelete, mode: x[1].mode })) return;
    // 4. group edits, then 5. the mode, 6. activations, 7. sound / stop the alarm, 8. restart
    const gw = entries.filter(([, e]) => ['gdel', 'gupd', 'gnew'].includes(e.kind));
    for (const [k, e] of gw) {
      const base = { device_id: dev };
      try {
        if (e.kind === 'gdel') await H.callWS({ type: WS.gdelete, ...base, group_id: e.gid });
        if (e.kind === 'gupd') await H.callWS({ type: WS.gupdate, ...base, group_id: e.gid, name: e.name, members: e.members });
        if (e.kind === 'gnew') await H.callWS({ type: WS.gcreate, ...base, name: e.name, members: e.members });
        this._pend.delete(k);
      } catch (err) { fail([k], err); }
    }
    if (gw.length) { this._gview = null; await this._loadGroups(); }
    const md = entries.find(([, e]) => e.kind === 'mode');
    if (md) {
      try { await H.callService('select', 'select_option', { entity_id: this._hb.security_mode, option: this._modeOption(md[1].value) }); } catch (err) { fail([md[0]], err); }
    }
    for (const [k, e] of entries.filter(([, x]) => x.kind === 'gact')) {
      try {
        const r = await H.callWS({ type: WS.gact, device_id: dev, group_id: e.gid });
        this._gres[e.gid] = (r && r.results) || [];
        if (this._gres[e.gid].some(x => x.outcome === 'failed')) fail([k], { message: 'Not applied' }); else this._pend.delete(k);
      } catch (err) { fail([k], err); }
    }
    const sr = entries.find(([, e]) => e.kind === 'siren');
    if (sr) {
      try {
        const r = await H.callWS({ type: WS.alarm, device_id: dev, action: sr[1].action, ...(sr[1].action === 'sound' ? { seconds: SIREN_S } : {}) });
        this._pend.delete(sr[0]); if (r && r.modes) this._adopt(r);
      } catch (err) { fail([sr[0]], err); }
    }
    const rs = entries.find(([, e]) => e.kind === 'restart');
    if (rs) { try { await H.callWS({ type: WS.restart, device_id: dev }); this._pend.delete(rs[0]); } catch (err) { fail([rs[0]], err); } }
    if (entries.some(([, e]) => e.kind === 'gact')) this._loadGroups();
    this._settle();
    this._render();
  }
  // After a config_changed refusal: the edits that still apply to the fresh payload go back to staging
  _restage(list) {
    for (const [k, e] of list) {
      this._pend.delete(k);
      if (e.kind === 'flag') { const l = this._liveFlag(e.mode, e.dev, e.flag); if (l === undefined || l === e.value) continue; }
      if (e.kind === 'dval') { const l = this._liveModeDelay(e.mode, e.which); if (l === undefined || l === e.value) continue; }
      if (e.kind === 'ddev') { const l = this._liveDelay(e.mode, e.dev, e.which); if (l === undefined || (l > 0) === e.value) continue; }
      if (e.kind === 'sched' && schedKey(e.value) === schedKey(this._liveSched())) continue;
      if ((e.kind === 'mren' || e.kind === 'mdel') && !this._mode(e.mode)) continue;
      this._stg.set(k, e);
    }
  }
  _settle() {
    if (!this._hb || !this._hass) return;
    const now = Date.now();
    let next = Infinity;
    for (const [k, p] of [...this._pend]) {
      if (p.status !== 'pending') continue;
      const e = p.entry;
      let done = false;
      if (e.kind === 'flag') done = this._liveFlag(e.mode, e.dev, e.flag) === e.value;
      else if (e.kind === 'dval') done = (this._liveModeDelay(e.mode, e.which) || 0) === e.value;
      else if (e.kind === 'ddev') { const l = this._liveDelay(e.mode, e.dev, e.which); done = l !== undefined && (l > 0) === e.value; }
      else if (e.kind === 'ent') { const s = this._st(e.id); done = !!s && (s.state === e.value || (!isNaN(Number(e.value)) && Number(s.state) === Number(e.value))); }
      else if (e.kind === 'mode') { const s = this._st(this._hb.security_mode); done = this._hb.guard_mode === e.value || (!!s && s.state === this._modeOption(e.value)); }
      else if (e.kind === 'mnew') done = this._hb.modes.some(m => m.custom && m.name === e.name);
      else if (e.kind === 'mren') { const m = this._mode(e.mode); done = !!m && m.name === e.name; }
      else if (e.kind === 'mdel') done = !this._mode(e.mode);
      else if (e.kind === 'sched') { const l = this._liveSched(); done = schedKey({ ...e.value, default: e.value.default || l.default }) === schedKey(l); }
      else if (e.kind === 'xcams') done = JSON.stringify(this._xcLive().cams) === JSON.stringify(e.value);
      else if (e.kind === 'xgroups') done = JSON.stringify(this._xcLive().groups) === JSON.stringify(e.value.map(g => g.filter(Boolean)).filter(g => g.length));
      else continue; // group writes, the alarm and restart settle on their answer
      if (done) this._pend.delete(k);
      else if (now - p.t >= PENDING_MS) { p.status = 'failed'; p.msg = null; }
      else next = Math.min(next, p.t + PENDING_MS);
    }
    clearTimeout(this._pt);
    if (next < Infinity) this._pt = setTimeout(() => { this._settle(); this._render(); }, Math.max(50, next - now + 20));
  }

  // ---------- events ----------
  _bind() {
    const R = this.shadowRoot, cc = R.querySelector('.cc');
    cc.addEventListener('click', e => this._click(e));
    cc.addEventListener('keydown', e => this._key(e));
    R.querySelectorAll('input[type=range]').forEach((r) => {
      r.addEventListener('input', () => {
        const v = this._rangeValue(r);
        r.style.setProperty('--pos', `${((Number(r.value) - r.min) / (r.max - r.min || 1)) * 100}%`);
        const o = r.closest('.row').querySelector('.rval'); if (o) o.textContent = `${v}${r.dataset.unit ? ` ${r.dataset.unit}` : ''}`;
      });
      r.addEventListener('change', () => this._rangeStage(r));
    });
    const df = R.querySelector('input[data-ddf]');
    if (df) df.addEventListener('input', () => { this._ddq = df.value; this._ddApply(); });
    const gn = R.querySelector('input[data-gname]');
    if (gn) gn.addEventListener('change', () => { this._stageGroup({ name: gn.value.trim() }); this._fk = 'g-name'; this._render(); });
    R.querySelectorAll('input[data-mren], input[data-mnew]').forEach((inp) => {
      inp.addEventListener('change', () => { this._stageModeName(inp.dataset.mren === undefined ? null : inp.dataset.mren, inp.value); this._fk = inp.dataset.focus; this._commit = inp.dataset.focus; this._render(); });
      inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); inp.blur(); } });
    });
  }
  _rangeValue(r) { return r.dataset.steps ? JSON.parse(r.dataset.steps)[Number(r.value)] : Number(r.value); }
  _rangeStage(r) {
    const v = this._rangeValue(r);
    if (r.dataset.dmode) this._stageDelayValue(r.dataset.dmode, r.dataset.which, v);
    else this._stageEnt(r.dataset.ent, v, this._secOfEnt(r.dataset.ent));
    this._fk = r.dataset.focus; this._render();
  }
  _secOfEnt(id) { return (this._lay || []).find(s => s.entity_ids.includes(id)); }
  _click(e) {
    const t = e.target.closest('[data-act],[data-sec],[data-node],[data-hub],[data-lens],[data-lstep],[data-pg],[data-cell],[data-dcell],[data-dsw],[data-stab],[data-tg],[data-opt],[data-ddo],[data-dd],[data-menu],[data-mi],[data-gact],[data-gedit],[data-gback],[data-gmode],[data-gdel],[data-gnew],[data-more],'
      + '[data-madd],[data-mdel],[data-slot],[data-sday],[data-sact],[data-xall],[data-xcam],[data-xg],[data-xgdel],[data-xgadd]');
    if (!t || t.disabled || t.getAttribute('aria-disabled') === 'true') return;
    const ds = t.dataset, fk = ds.focus;
    if (ds.act === 'cancel') return this._cancel();
    if (ds.act === 'set') return this._set(false);
    if (ds.act === 'cf-set') return this._set(true);
    if (ds.act === 'cf-back') { this._confirm = false; this._fk = 'act-set'; return this._render(); }
    if (ds.act === 'disarm') { this._disarm(); return; }
    if (ds.act === 'stopalarm') { this._stopAlarm(); return; }
    if (ds.act === 'm-close') { this._medit = false; this._mnew = false; this._fk = 'menu'; return this._render(); }
    if (ds.madd !== undefined) { this._mnew = true; this._fk = 'm-new'; return this._render(); }
    if (ds.mdel) { this._stageModeDel(ds.mdel); this._fk = fk; return this._render(); }
    if (ds.slot !== undefined) { this._loadSlot(Number(ds.slot)); this._fk = 'dd-slotpick'; return this._render(); }
    if (ds.sday !== undefined) { const f = this._sf(), wd = Number(ds.sday); f.days = f.days.includes(wd) ? f.days.filter(x => x !== wd) : [...f.days, wd]; this._fk = fk; return this._render(); }
    if (ds.sact) { this._slotAct(ds.sact); this._fk = ds.sact === 'del' ? 'sf-add' : fk; return this._render(); }
    if (ds.xall !== undefined || ds.xcam) {
      const live = this._xcLive().cams, cur = this._val('xc|cams', live), ids = this._xcCams().map(d => d.device_id);
      let next;
      if (ds.xall !== undefined) next = cur === null ? ids : null;
      else { const l = cur === null ? ids : cur.filter(x => ids.includes(x)); next = l.includes(ds.xcam) ? l.filter(x => x !== ds.xcam) : [...l, ds.xcam]; }
      if (this._xcShort(next)) return;
      this._stageX('xc|cams', 'xcams', next, live, 'cross_camera'); this._fk = fk; return this._render();
    }
    if (ds.xg || ds.xgdel !== undefined || ds.xgadd !== undefined) {
      const live = this._xcLive().groups, g = JSON.parse(JSON.stringify(this._val('xc|groups', live)));
      if (ds.xg) { const [gi, id] = [Number(ds.xg.split('|')[0]), ds.xg.slice(ds.xg.indexOf('|') + 1)]; g[gi] = g[gi].includes(id) ? g[gi].filter(x => x !== id) : [...g[gi], id]; }
      else if (ds.xgdel !== undefined) g.splice(Number(ds.xgdel), 1);
      else g.push([]);
      this._stageX('xc|groups', 'xgroups', g, live, 'cross_camera_groups');
      this._fk = ds.xgdel !== undefined ? 'xg-add' : fk; return this._render();
    }
    if (ds.act === 'retry') { this._err = null; this._render(); this._load(); return; }
    if (ds.more) { this.dispatchEvent(new CustomEvent('hass-more-info', { detail: { entityId: ds.more }, bubbles: true, composed: true })); return; }
    if (ds.menu !== undefined) { this._menu = !this._menu; this._dd = null; this._fk = this._menu ? 'mi-first' : 'menu'; return this._render(); }
    if (ds.mi) return this._menuPick(ds.mi);
    if (ds.gact) { const k = `ga|${ds.gact}`; this._toggleStage(k, { kind: 'gact', gid: ds.gact, alarm: this._affects('arming_groups') }); this._menu = false; this._fk = t.closest('.menu') ? 'menu' : fk; return this._render(); }
    if (ds.sec) { this._open = this._open === ds.sec ? null : ds.sec; this._dd = null; if (this._open === 'devices' && !this._dev && this._devs().length) this._pick(this._devs()[this._page * this._cap()] ? this._devs()[this._page * this._cap()].device_id : this._devs()[0].device_id); this._fk = fk; return this._render(); }
    if (ds.node) { this._pick(ds.node); this._open = 'devices'; this._gview = null; this._fk = fk; return this._render(); }
    if (ds.hub !== undefined) { this._open = this._open === 'status' ? null : 'status'; this._fk = fk; return this._render(); }
    if (ds.lens) { this._preview = ds.lens; this._fk = fk; return this._render(); }
    if (ds.lstep) { const ids = this._lensIds(); const i = ids.indexOf(this._previewMode()); this._preview = ids[(i + Number(ds.lstep) + ids.length) % ids.length]; this._fk = fk; return this._render(); }
    if (ds.pg) { const n = this._pages(); this._page = ds.pg === 'prev' ? (this._page + n - 1) % n : ds.pg === 'next' ? (this._page + 1) % n : Number(ds.pg); this._fk = fk; return this._render(); }
    if (ds.cell) { const [mode, dev, flag] = ds.cell.split('|'); this._stageFlag(mode, dev, flag); this._fk = fk; return this._render(); }
    if (ds.dcell) { const [mode, which] = ds.dcell.split('|'); this._dsel[ds.dev] = { mode, which }; this._preview = mode; this._fk = fk; return this._render(); }
    if (ds.dsw) { const [mode, which, dev] = ds.dsw.split('|'); this._stageDelayDev(mode, dev, which, !this._devDelayOn(mode, dev, which)); this._fk = fk; return this._render(); }
    if (ds.stab) { this._stab = ds.stab; this._dd = null; this._fk = fk; return this._render(); }
    if (ds.tg) { const s = this._st(ds.tg); const cur = this._val(`e|${ds.tg}`, s && s.state); this._stageEnt(ds.tg, cur === 'on' ? 'off' : 'on', this._secOfEnt(ds.tg)); this._fk = fk; return this._render(); }
    if (ds.opt !== undefined && ds.ent) { this._stageEnt(ds.ent, ds.opt, this._secOfEnt(ds.ent)); this._fk = fk; return this._render(); }
    if (ds.dd) { this._dd = this._dd === ds.dd ? null : ds.dd; this._ddq = ''; this._menu = false; this._fk = this._dd ? `ddo-${ds.dd}-cur` : fk; return this._render(); }
    if (ds.ddo !== undefined) {
      const key = this._dd; this._dd = null; this._ddq = '';
      if (key === 'devpick') { this._pick(ds.ddo); this._fk = 'dd-devpick'; }
      else if (key === 'slotpick') { this._loadSlot(ds.ddo === 'new' ? null : Number(ds.ddo)); this._fk = 'dd-slotpick'; }
      else if (key === 'sf|def') { this._stageSched({ ...this._schedVal(), default: ds.ddo }); this._fk = `dd-${key}`; }
      else if (key && key.startsWith('sf|')) { this._sf()[key.slice(3)] = ds.ddo; this._fk = `dd-${key}`; }
      else if (key && key.startsWith('sel|')) { const id = key.slice(4); this._stageEnt(id, ds.ddo, this._secOfEnt(id)); this._fk = `dd-${key}`; }
      return this._render();
    }
    if (ds.gedit) { this._gview = ds.gedit; this._loadLogs(ds.gedit); this._fk = 'g-back'; return this._render(); }
    if (ds.gnew !== undefined) { this._gview = 'new'; this._fk = 'g-name'; return this._render(); }
    if (ds.gback !== undefined) { const g = this._gview; this._gview = null; this._fk = g === 'new' ? 'g-new' : `g-edit-${g}`; return this._render(); }
    if (ds.gmode) {
      const [dev, mode] = ds.gmode.split('|'); const cur = this._groupDraft();
      const members = cur.members.filter(m => m.device_id !== dev); if (mode) members.push({ device_id: dev, mode });
      this._stageGroup({ members }); this._fk = fk; return this._render();
    }
    if (ds.gdel) { this._toggleStage(`gd|${ds.gdel}`, { kind: 'gdel', gid: ds.gdel }); this._fk = fk; return this._render(); }
  }
  _menuPick(mi) {
    this._menu = false;
    if (mi === 'refresh') { this._fk = 'menu'; this._render(); this._reload(); if (this._hb) this._loadLayout(); if (this._shows('arming_groups')) this._loadGroups(); return; }
    if (mi === 'restart') { this._toggleStage('restart', { kind: 'restart', alarm: this._affects('restart') }); this._fk = 'menu'; return this._render(); }
    if (mi === 'siren') { this._toggleStage('siren', { kind: 'siren', action: this._triggered() ? 'stop' : 'sound', alarm: this._affects('sound_alarm') }); this._fk = 'menu'; return this._render(); }
    if (mi === 'modes') { this._medit = true; this._confirm = false; const c = this._hb.modes.find(m => m.custom); this._fk = c ? `m-name-${c.id}` : 'm-add'; return this._render(); }
    if (mi.startsWith('m:')) {
      const id = mi.slice(2);
      if (id === this._hb.guard_mode) this._stg.delete('mode'); else this._stage('mode', { kind: 'mode', value: id, alarm: this._affects('guard_mode') });
      this._fk = 'menu'; return this._render();
    }
  }
  async _disarm() {
    const id = this._hb.alarm_panel;
    this._pend.set('disarm', { entry: { kind: 'disarm' }, status: 'pending', t: Date.now() });
    this._render();
    try { await this._hass.callService('alarm_control_panel', 'alarm_disarm', { entity_id: id }); this._pend.delete('disarm'); } catch (err) { this._pend.set('disarm', { entry: { kind: 'disarm' }, status: 'failed', t: Date.now(), msg: err && err.message }); }
    this._render();
  }
  // Stop alarm while it sounds: at once, like Disarm
  async _stopAlarm() {
    const P = st => ({ entry: { kind: 'stopalarm' }, status: st, t: Date.now() });
    this._pend.set('stopalarm', P('pending'));
    this._render();
    try {
      const r = await this._hass.callWS({ type: WS.alarm, device_id: this._hb.device_id, action: 'stop' });
      this._pend.delete('stopalarm'); if (r && r.modes) this._adopt(r);
    } catch (err) { this._pend.set('stopalarm', { ...P('failed'), msg: err && err.message }); this._say(err && err.message); }
    this._render();
  }
  _key(e) {
    const t = e.target;
    if ((e.key === 'Enter' || e.key === ' ') && t.matches('g[role=button]')) { e.preventDefault(); t.dispatchEvent(new MouseEvent('click', { bubbles: true, composed: true })); return; }
    if (e.key === 'Escape' && (this._menu || this._confirm || this._dd || this._medit)) {
      e.preventDefault();
      // the topmost layer closes: a list, the confirm, the modes editor, the menu
      if (this._dd) { this._fk = `dd-${this._dd}`; this._dd = null; this._ddq = ''; } else if (this._confirm) { this._confirm = false; this._fk = 'act-set'; } else if (this._medit) { this._medit = false; this._mnew = false; this._fk = 'menu'; } else { this._menu = false; this._fk = 'menu'; }
      this._render(); return;
    }
    // a list's filter field: Down goes to the first match, Enter picks it
    if (t.matches('input[data-ddf]')) {
      const first = t.closest('.ddl').querySelector('[role=option]:not([hidden])');
      if (e.key === 'ArrowDown' || e.key === 'PageDown') { e.preventDefault(); if (first) first.focus(); }
      if (e.key === 'Enter') { e.preventDefault(); if (first) first.click(); }
      return;
    }
    const list = t.closest('[role=menu],[role=listbox]');
    if (list && ['ArrowDown', 'ArrowUp', 'Home', 'End', 'PageDown', 'PageUp'].includes(e.key)) {
      e.preventDefault();
      const it = [...list.querySelectorAll('[role^=menuitem]:not([aria-disabled=true]),[role=option]:not([hidden])')]; const i = it.indexOf(t);
      const f = list.parentElement.querySelector('.ddf');
      if (f && e.key === 'ArrowUp' && i === 0) { f.focus(); return; }
      const j = e.key === 'Home' ? 0 : e.key === 'End' ? it.length - 1 : e.key === 'PageDown' ? Math.min(it.length - 1, i + DD_ROWS - 1)
        : e.key === 'PageUp' ? Math.max(0, i - DD_ROWS + 1) : (i + (e.key === 'ArrowDown' ? 1 : it.length - 1)) % it.length;
      if (it[j]) it[j].focus();
      return;
    }
    // type-ahead: the next option whose text starts with the typed letters
    if (list && list.getAttribute('role') === 'listbox' && e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey && e.key !== ' ') {
      const now = Date.now(), b = this._ddt; b.s = now - b.t > DD_TYPE_MS ? e.key.toLowerCase() : b.s + e.key.toLowerCase(); b.t = now;
      const it = [...list.querySelectorAll('[role=option]:not([hidden])')], i = it.indexOf(t);
      const order = [...it.slice(i + (b.s.length > 1 ? 0 : 1)), ...it.slice(0, i + (b.s.length > 1 ? 0 : 1))];
      const hit = order.find(o => (o.getAttribute('aria-label') || '').toLowerCase().startsWith(b.s));
      if (hit) { e.preventDefault(); hit.focus(); }
      return;
    }
    const tabs = t.closest('[role=tablist]');
    if (tabs && t.getAttribute('role') === 'tab' && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
      e.preventDefault();
      const it = [...tabs.querySelectorAll('[role=tab]')]; const i = it.indexOf(t);
      const j = e.key === 'Home' ? 0 : e.key === 'End' ? it.length - 1 : (i + (e.key === 'ArrowRight' ? 1 : it.length - 1)) % it.length;
      it[j].focus(); if (tabs.classList.contains('stabs')) it[j].click();
      return;
    }
    if (t.closest('.pager') && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
      e.preventDefault(); const n = this._pages(); this._page = (this._page + (e.key === 'ArrowRight' ? 1 : n - 1)) % n; this._fk = t.dataset.focus; this._render();
    }
  }
  _pick(id) { this._dev = id; const i = this._devs().findIndex(d => d.device_id === id); if (i >= 0) this._page = Math.floor(i / this._cap()); }
  _groupDraft() {
    const gid = this._gview;
    if (gid === 'new') return this._stg.get('gn') || { name: '', members: [] };
    const g = this._group(gid); const s = this._stg.get(`gu|${gid}`);
    return s || { name: g ? g.name : '', members: g ? g.members.filter(m => m.device_id).map(m => ({ device_id: m.device_id, mode: m.mode })) : [] };
  }
  _stageGroup(patch) {
    const gid = this._gview;
    if (gid === 'new') { const cur = this._stg.get('gn') || { kind: 'gnew', name: '', members: [] }; this._stg.set('gn', { ...cur, ...patch }); return; }
    const cur = this._groupDraft(); const next = { kind: 'gupd', gid, ...cur, ...patch };
    const g = this._group(gid);
    const same = g && next.name === g.name && JSON.stringify(next.members.map(m => `${m.device_id}|${m.mode}`).sort()) === JSON.stringify(g.members.filter(m => m.device_id).map(m => `${m.device_id}|${m.mode}`).sort());
    if (same) this._stg.delete(`gu|${gid}`); else this._stg.set(`gu|${gid}`, next);
    this._confirm = false;
  }

  // ---------- rendering ----------
  _cap() { return (this._w || 400) < NARROW_W ? CAP_NARROW : CAP_WIDE; }
  _pages() { return Math.max(1, Math.ceil(this._devs().length / this._cap())); }
  _font(w = 400, px = 12) { if (!this._ff) this._ff = getComputedStyle(this).fontFamily || 'Roboto, sans-serif'; return `${w} ${px}px ${this._ff}`; }
  _fs() { const v = parseFloat(getComputedStyle(this).getPropertyValue('--ha-font-size-s')); return v > 0 ? v : 12; }
  _render() {
    const R = this.shadowRoot;
    const prevFocus = this._fk || (R.activeElement && R.activeElement.dataset && R.activeElement.dataset.focus);
    this._fk = null;
    if (!this._hass || !this._config) return;
    const W = (this._w || 400) - (this._inset || 0);
    const ae = R.activeElement, typing = ae && ae.tagName === 'INPUT' && ae.type === 'text' && ae.dataset.focus ? { k: ae.dataset.focus, v: ae.value, a: ae.selectionStart, b: ae.selectionEnd } : null;
    let body;
    if (!this._devId()) body = this._shell('Choose a HomeBase in the card editor');
    else if (this._err) body = this._shell(this._err.code === 'not_found' ? 'This device is not a eufy station' : 'The HomeBase did not load', true);
    else if (!this._hb) body = this._shell('Loading');
    else {
      if (this._page >= this._pages()) this._page = 0;
      body = `${this._header()}${this._picture(W)}${this._toggles(W)}${this._section(W)}`;
    }
    R.innerHTML = `<style>${CSS}</style><ha-card><div class="cc${this._visible === false ? ' paused' : ''}">${body}</div></ha-card>`;
    this._sk = this._stateKey();
    this._bind();
    this._place();
    // the header buttons (Set / ✕, Stop alarm / Disarm) take the name's room on a narrow card: hide it, never cut it
    const hd = R.querySelector('.hdr'), nm = R.querySelector('.hdr .name');
    if (hd && nm && !R.querySelector('.hdr .acts.off') && nm.scrollWidth > nm.clientWidth + 1) hd.classList.add('tight');
    if (prevFocus) this._focus(prevFocus);
    if (typing && typing.k !== this._commit) { const el = R.querySelector(`input[data-focus="${cssEsc(typing.k)}"]`); if (el && R.activeElement === el && !el.disabled) { el.value = typing.v; try { el.setSelectionRange(typing.a, typing.b); } catch (x) { /* not a text field */ } } }
    this._commit = null;
    // the drawing is sized to the content box: ha-card's border takes from the host width
    const cw = R.querySelector('.cc').clientWidth;
    if (cw && this._w && Math.abs(cw - W) >= 1 && !this._insetting) { this._inset = this._w - cw; this._insetting = true; this._render(); this._insetting = false; }
  }
  _focus(k) {
    const R = this.shadowRoot;
    const find = key => { const el = R.querySelector(`[data-focus="${cssEsc(key)}"]`); return el && !el.disabled && !el.closest('[hidden-pane],[inert]') ? el : null; };
    let el = k === 'mi-first' ? (R.querySelector('.menu .mi.cur') || R.querySelector('.menu .mi:not([aria-disabled=true])')) : k.endsWith('-cur') && k.startsWith('ddo-') ? (R.querySelector('.ddl .ddf') || R.querySelector('.ddl [aria-selected=true]') || R.querySelector('.ddl [role=option]')) : find(k);
    if (!el && (k === 'act-set' || k === 'act-cancel' || k === 'cf-set' || k === 'cf-back')) el = find('menu');
    if (!el && k.startsWith('pg-')) el = find('pg-next') || find('pg-prev');
    if (el) el.focus({ preventScroll: true });
  }
  // Menu and dropdowns stay inside the card (ha-card clips): cap their height to the room below or above
  _place() {
    const R = this.shadowRoot, card = R.querySelector('ha-card'); if (!card) return;
    const cr = card.getBoundingClientRect();
    const m = R.querySelector('.menu');
    if (m) { const r = m.getBoundingClientRect(); m.style.maxHeight = `${Math.max(80, Math.floor(cr.bottom - r.top - 8))}px`; }
    const l = R.querySelector('.ddl');
    if (l) { this._ddApply(); this._ddPlace(l, cr); }
    else this._ddTries = 0;
  }
  // An open list: DD_MIN_ROWS..DD_ROWS rows (all when fewer) inside the card; downward when that fits below the field,
  // else upward when it fits above, else downward with the card grown by the missing height while the list is open
  _ddPlace(l, cr) {
    const ii = l.querySelector('.ddi'), br = l.parentElement.getBoundingClientRect();
    for (const g of this.shadowRoot.querySelectorAll('.ddgrow')) g.remove();
    const w = Math.min(Math.max(br.width, DD_MIN_W), cr.width - 16), left = Math.min(Math.max(br.right - w, cr.left + 8), cr.right - 8 - w);
    Object.assign(l.style, { left: `${Math.round(left - br.left)}px`, right: 'auto', width: `${Math.round(w)}px`, maxHeight: 'none', height: 'auto' });
    const opts = [...ii.querySelectorAll('[role=option]')], n = opts.length, o1 = opts.find(o => !o.hidden);
    // A list opened before the browser laid it out (an off-screen dashboard section) measures 0: place it again next frame
    if (o1 && !o1.offsetHeight) {
      if ((this._ddTries = (this._ddTries || 0) + 1) <= 10) requestAnimationFrame(() => this._place());
      return;
    }
    this._ddTries = 0;
    const row = (o1 ? o1.offsetHeight : 34) + 1;
    const chrome = l.offsetHeight - ii.offsetHeight;
    const full = chrome + row * Math.min(n, DD_ROWS) - 1, need = chrome + row * Math.min(n, DD_MIN_ROWS) - 1;
    const below = Math.floor(cr.bottom - br.bottom - 8), above = Math.floor(br.top - cr.top - 8);
    let H = full, up = false, grow = 0;
    if (below >= full) H = full;
    else if (above >= full) up = true;
    else if (below >= need && below >= above) H = below;
    else if (above >= need) { up = true; H = above; }
    else grow = full - below;
    l.classList.toggle('up', up);
    // a filtered list keeps its height while the matches change
    l.style[l.classList.contains('filt') ? 'height' : 'maxHeight'] = `${H}px`;
    if (grow) { const g = document.createElement('div'); g.className = 'ddgrow'; g.style.height = `${grow}px`; this.shadowRoot.querySelector('.cc').appendChild(g); }
    const sel = ii.querySelector('[aria-selected=true]:not([hidden])'); if (sel) ii.scrollTop = Math.max(0, sel.offsetTop - (ii.clientHeight - sel.offsetHeight) / 2);
  }
  // The filter text hides the options that do not contain it
  _ddApply() {
    const R = this.shadowRoot, ii = R.querySelector('.ddl .ddi'); if (!ii) return;
    const q = this._ddq.trim().toLowerCase();
    let shown = 0;
    ii.querySelectorAll('[role=option]').forEach((o) => { const h = !!q && !(o.getAttribute('aria-label') || '').toLowerCase().includes(q); o.hidden = h; if (!h) shown += 1; });
    ii.querySelector('.ddn').hidden = shown > 0;
  }
  _shell(msg, retry) {
    const nm = this._config.name || 'HomeBase';
    return `<div class="hdr"><div class="tline"><div class="title"><div class="bwrap"><span class="mbtn m-pending">${icon('shield-outline')}</span></div>
      <div class="htext"><div class="name">${esc(nm)}</div><div class="hsub"><span class="st">${esc(msg)}</span></div></div></div></div></div>
      <div class="pic shell" style="height:${166 + LENS_H}px">${retry ? `<button type="button" class="btn" data-act="retry" data-focus="retry" aria-label="Load again">${icon('refresh')}<span>Retry</span></button>` : ''}</div>`;
  }

  _header() {
    const hb = this._hb, trig = this._triggered();
    const ms = this._stg.get('mode'), mp = this._pend.get('mode');
    const g = hb.guard_mode, eff = hb.effective_mode;
    const fam = trig ? 'triggered' : this._fam(g);
    const btnFam = ms ? this._fam(ms.value) : fam;
    const devs = this._devs(), online = devs.filter(d => this._online(d)).length, off = devs.length - online;
    let word = this._mname(g), wcls = '';
    if ((g === 'schedule' || g === 'geofencing') && eff && eff !== g) word = `${this._mname(g)} · ${this._mname(eff)}`;
    if (hb.connected === false) { word = 'Not connected'; wcls = ' err'; }
    if (trig) { word = 'Alarm!'; wcls = ' err'; }
    if (ms) { word = `${this._mname(g)} → ${this._mname(ms.value)}`; wcls = ' staged'; }
    else if (mp && mp.status === 'pending') { word = `${this._mname(mp.entry.value)} · sending`; wcls = ' pending'; }
    else if (mp && mp.status === 'failed') { word = `${this._mname(mp.entry.value)} · Not applied`; wcls = ' err'; }
    const dp = this._pend.get('disarm'), sp = this._pend.get('stopalarm');
    if (dp && dp.status === 'failed') { word = 'Disarm · Not applied'; wcls = ' err'; }
    else if (sp && sp.status === 'failed') { word = 'Stop alarm · Not applied'; wcls = ' err'; }
    const parts = [`<span class="g"><span class="st${wcls}">${esc(word)}</span></span>`];
    if (this._note === 'changed') parts.push('<span class="g"><i class="sep">·</i><span class="warn">Changed elsewhere, review</span></span>');
    const nf = [...this._pend.entries()].filter(([k, p]) => p.status === 'failed' && !['mode', 'disarm', 'stopalarm'].includes(k)).length;
    if (nf && this._note !== 'changed') parts.push(`<span class="g"><i class="sep">·</i><span class="err">${nf} not applied</span></span>`);
    parts.push(`<span class="g"><i class="sep">·</i><span class="${off ? 'offn' : ''}">${off ? `${off} offline` : `${online} online`}</span></span>`);
    const n = this._stg.size;
    const label = `${this._config.name || hb.name}: ${word}${off ? `, ${off} devices offline` : ''}. Security mode menu`;
    const badge = (g === 'schedule' || g === 'geofencing') && !ms && !trig ? `<i class="badge" aria-hidden="true">${icon(MODE_ICON[g])}</i>` : '';
    const acts = [], stop = trig && this._shows('sound_alarm') && this._can('sound_alarm'), wide = (this._w || 400) >= 560;
    if (stop) acts.push(`<button class="btn alarm${wide ? '' : ' icon'}" type="button" data-act="stopalarm" data-focus="stopalarm" aria-label="Stop the alarm sound now"${sp && sp.status === 'pending' ? ' disabled' : ''}>${icon('bell-off')}${wide ? '<span>Stop alarm</span>' : ''}</button>`);
    // under 360 px with Set / ✕ beside it, Disarm is its icon so the four buttons leave the mode button free
    const dIcon = n && stop && (this._w || 400) < 360;
    if (trig && hb.alarm_panel) acts.push(`<button class="btn alarm${dIcon ? ' icon' : ''}" type="button" data-act="disarm" data-focus="disarm" aria-label="Disarm the HomeBase now"${dp && dp.status === 'pending' ? ' disabled' : ''}>${icon('shield-off')}${dIcon ? '' : '<span>Disarm</span>'}</button>`);
    if (n) {
      acts.push(`<button class="btn icon" type="button" data-act="cancel" data-focus="act-cancel" aria-label="Cancel the changes">${icon('close')}</button>`);
      const blk = this._setBlock();
      acts.push(`<button class="btn primary${this._alarmEntries().length ? ' warn' : ''}" type="button" data-act="set" data-focus="act-set" aria-label="${esc(blk ? `Set refused: ${blk}` : `Set: ${[...this._stg.entries()].map(x => this._effect(x)[1]).join('; ')}`)}"${blk ? ' disabled' : ''}>Set<span class="cnt${n > 1 ? '' : ' none'}" aria-hidden="true">${n > 1 ? n : 0}</span></button>`);
    }
    return `<div class="hdr${n ? ' staging' : ''}${trig ? ' trig' : ''}${stop ? (wide ? ' stopw' : ' stop') : ''}${dIcon ? ' dicon' : ''}"><div class="tline"><div class="title">
      <div class="bwrap"><button type="button" class="mbtn m-${btnFam}${ms ? ' staged' : ''}${mp && mp.status === 'failed' ? ' failed' : ''}${trig ? ' alarm' : ''}" data-menu data-focus="menu" aria-haspopup="menu" aria-expanded="${this._menu}" aria-label="${esc(label)}">${icon(trig ? 'bell-ring' : ms ? this._micon(ms.value) : this._micon(g === 'schedule' || g === 'geofencing' ? (eff || 'home') : g))}${badge}</button>${this._menu ? this._menuHtml() : ''}</div>
      <div class="htext"><div class="name" aria-label="${esc(this._config.name || hb.name)}">${esc(this._config.name || hb.name)}</div>
      <div class="hsub">${parts.join('')}</div></div></div>
      <div class="acts${acts.length ? '' : ' off'}"${acts.length ? '' : ' inert aria-hidden="true"'}>${acts.join('')}</div>
    </div></div>`;
  }
  _menuHtml() {
    const hb = this._hb, ms = this._stg.get('mode');
    const items = [];
    if (this._shows('guard_mode')) {
      const can = this._can('guard_mode') && !!hb.security_mode;
      hb.modes.filter(m => m.settable).forEach((m) => {
        const cur = hb.guard_mode === m.id, sel = ms && ms.value === m.id;
        const sub = m.id === 'schedule' && cur && hb.effective_mode && hb.effective_mode !== 'schedule' ? `${this._mname(hb.effective_mode)} now` : '';
        items.push(`<button type="button" role="menuitemradio" aria-checked="${sel || (cur && !ms)}" class="mi${cur ? ' cur' : ''}${sel ? ' sel' : ''}" data-mi="m:${esc(m.id)}" data-focus="mi-m-${esc(m.id)}"${can || cur ? '' : ' aria-disabled="true"'} aria-label="${esc(`${m.name}${sub ? `, ${sub}` : ''}${cur ? ', current' : ''}${can ? '' : ': switching not available yet'}`)}">${icon(this._micon(m.id))}<span>${esc(m.name)}</span>${sub ? `<span class="later">${esc(sub)}</span>` : ''}${(sel || (cur && !ms)) ? icon('check', 'chk') : ''}</button>`);
      });
      if (this._sup('guard_mode').grade === 'declared') items.push('<div class="mnote" role="note">Not tested on this model</div>');
    }
    const gs = this._shows('arming_groups') && this._groups && this._groups.groups ? this._groups.groups : [];
    gs.forEach((g, i) => {
      const st = this._stg.has(`ga|${g.group_id}`);
      items.push(`<button type="button" role="menuitemcheckbox" aria-checked="${st}" class="mi${i ? '' : ' sep'}${st ? ' sel' : ''}" data-gact="${esc(g.group_id)}" data-focus="mi-g-${esc(g.group_id)}"${this._can('arming_groups') ? '' : ' aria-disabled="true"'} aria-label="${esc(`Activate ${g.name}${g.active ? ', active' : ''}`)}">${icon(g.preset === 'leaving' ? 'home-export-outline' : g.preset === 'staying' ? 'home-import-outline' : 'account-group')}<span>${esc(g.name)}</span>${st ? icon('check', 'chk') : g.active ? `<span class="later">active</span>` : ''}</button>`);
    });
    const acts = [];
    if (this._shows('custom_modes')) {
      const ch = [...this._stg.keys()].some(k => k.startsWith('mc|'));
      acts.push(`<button type="button" role="menuitem" class="mi" data-mi="modes" data-focus="mi-modes" aria-label="${ch ? 'Edit the custom modes, changed' : 'Edit the custom modes'}">${icon('pencil')}<span>Edit modes</span>${ch ? '<span class="later">changed</span>' : ''}</button>`);
    }
    const sa = this._sup('sound_alarm');
    if (sa.grade !== 'unknown') {
      const on = this._can('sound_alarm'), st = this._stg.get('siren'), stopIt = st ? st.action === 'stop' : this._triggered();
      const lab = stopIt ? 'Stop alarm' : `Sound alarm ${SIREN_S} s`;
      acts.push(`<button type="button" role="menuitemcheckbox" aria-checked="${!!st}" class="mi${st ? ' sel' : ''}" data-mi="siren" data-focus="mi-siren"${on ? '' : ' aria-disabled="true"'} aria-label="${esc(`${stopIt ? 'Stop the HomeBase alarm' : `Sound the HomeBase alarm for ${SIREN_S} s`}${on ? '' : ': not available yet'}`)}">${icon(stopIt ? 'bell-off' : 'bullhorn')}<span>${esc(lab)}</span>${on ? (st ? icon('check', 'chk') : '') : '<span class="later">Not available yet</span>'}</button>`);
    }
    const rs = this._sup('restart');
    if (rs.grade !== 'unknown' || !(this._hb.support || {}).restart) {
      const on = rs.available === true, st = this._stg.has('restart');
      acts.push(`<button type="button" role="menuitemcheckbox" aria-checked="${st}" class="mi${st ? ' sel' : ''}" data-mi="restart" data-focus="mi-restart"${on ? '' : ' aria-disabled="true"'} aria-label="${on ? 'Restart the HomeBase' : 'Restart: not available yet'}">${icon('restart')}<span>Restart</span>${on ? (st ? icon('check', 'chk') : '') : '<span class="later">Not available yet</span>'}</button>`);
    }
    acts.push(`<button type="button" role="menuitem" class="mi" data-mi="refresh" data-focus="mi-refresh" aria-label="Refresh from the HomeBase">${icon('refresh')}<span>Refresh</span></button>`);
    acts[0] = acts[0].replace('class="mi', 'class="mi sep');
    items.push(...acts);
    return `<div class="menu" role="menu" aria-label="Security mode, groups and HomeBase actions">${items.join('')}</div>`;
  }

  // Slot positions on a page: side columns left and right of the hub, row-major order
  _geom(W) {
    const narrow = W < NARROW_W, cap = narrow ? CAP_NARROW : CAP_WIDE;
    const per = Math.min(this._devs().length, cap);
    const rows = per <= 2 ? 1 : narrow ? (per <= 4 ? 2 : 3) : 2;
    const cols = narrow || per <= 4 ? 1 : 2;
    const pad = 4, hubW = 2 * (HUB_R + 8);
    const slotW = Math.min(136, Math.floor((W - hubW - 2 * pad - 16) / (2 * cols)));
    const D = Math.max(rows * ROW_H, 166) + 12;
    const cx = W / 2, hubY = Math.round(D / 2) - 8;
    const xs = [];
    for (let c = 0; c < cols; c++) xs.push(pad + slotW / 2 + c * slotW);
    const order = [...xs, ...xs.map(x => W - x).reverse()];
    const slots = [];
    for (let r = 0; r < rows; r++) {
      const y = Math.round(D / 2 + (r - (rows - 1) / 2) * ROW_H - 16);
      for (const x of order) slots.push({ x: Math.round(x), y });
    }
    return { D, cx, hubY, slotW, slots: slots.slice(0, Math.max(per, 1)), rows, cols, cap };
  }
  _fit(t, maxW, font) {
    if (textW(t, font) <= maxW) return t;
    let lo = 0, hi = t.length;
    while (lo < hi) { const m = Math.ceil((lo + hi) / 2); if (textW(`${t.slice(0, m)}…`, font) <= maxW) lo = m; else hi = m - 1; }
    return `${t.slice(0, lo)}…`;
  }
  _lensIds() { const ids = this._tmodes().map(m => m.id); if (this._schedOk()) ids.push('schedule'); return ids; }
  _picture(W) {
    const hb = this._hb, G = this._geom(W), trig = this._triggered();
    const pm = this._previewMode(), sched = pm === 'schedule';
    const lensIds = this._lensIds(), showLens = lensIds.length > 0 && this._shows('mode_tables');
    const H = G.D + (showLens ? LENS_H : 0);
    const devs = this._devs(), cap = G.cap, pages = this._pages();
    const page = devs.slice(this._page * cap, this._page * cap + cap);
    const fs = this._fs(), nameFont = this._font(500, fs), txtFont = this._font(400, fs);
    const ms = this._stg.get('mode');
    const fam = trig ? 'triggered' : this._fam(hb.guard_mode);
    const hx = G.cx, hy = G.hubY;
    let links = '', nodes = '';
    page.forEach((d, i) => {
      const { x, y } = G.slots[i];
      const on = this._online(d);
      const flags = this._flagsOf(d);
      const act = flags.filter(f => this._flag(pm, d.device_id, f.id) === true).sort((a, b) => this._alarmFlag(b) - this._alarmFlag(a));
      const hubAlarm = on && trig && this._flag(pm, d.device_id, 'station_alarm') === true;
      const dx = x - hx, dy = y - hy, len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len;
      const x1 = hx + ux * (HUB_R + 5), y1 = hy + uy * (HUB_R + 5), x2 = x - ux * 25, y2 = y - uy * 25;
      links += `<line class="link${on ? '' : ' off'}" x1="${x2.toFixed(1)}" y1="${y2.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}"/>`;
      if (hubAlarm) links += `<line class="flow" x1="${x2.toFixed(1)}" y1="${y2.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}"/>`;
      const bat = typeof d.battery === 'number' ? d.battery : NaN, low = on && bat < 30;
      const maxW = G.slotW - 8;
      const nm = this._fit(d.name, maxW - (low ? 16 : 0), nameFont);
      const nw = textW(nm, nameFont);
      let row = '';
      const gy = y + 49;
      if (sched) row = '';
      else if (!on) row = `<text class="ntxt off" x="${x}" y="${y + 53}" text-anchor="middle">Offline</text>`;
      else {
        const delay = this._devDelay(pm, d.device_id, 'entry') || 0;
        const dtxt = delay > 0 ? `${delay}s` : '';
        const dw = dtxt ? 14 + textW(dtxt, txtFont) : 0;
        const gs = 14, gap = 4;
        let shown = act.length, more = '';
        const width = k => k * gs + Math.max(0, k - 1) * gap + (dtxt ? (k ? gap + 2 : 0) + dw : 0) + (k < act.length ? gap + textW(`+${act.length - k}`, txtFont) : 0);
        while (shown > 0 && width(shown) > maxW) shown -= 1;
        if (shown < act.length) more = `+${act.length - shown}`;
        const tw = width(shown);
        let gx = x - tw / 2;
        if (!act.length && !dtxt) row = `<text class="ntxt dim" x="${x}" y="${y + 53}" text-anchor="middle">–</text>`;
        act.slice(0, shown).forEach((f) => { row += svgIcon(FLAG_ICON[f.id] || 'alert-circle', gx + gs / 2, gy, gs, this._alarmFlag(f) ? 'al' : ''); gx += gs + gap; });
        if (more) { row += `<text class="ntxt" x="${gx.toFixed(1)}" y="${y + 53}">${more}</text>`; gx += textW(more, txtFont) + gap; }
        if (dtxt) { if (shown) gx += 2; row += svgIcon('timer-outline', gx + 6, gy, 12, 'dl'); row += `<text class="ntxt dl" x="${(gx + 14).toFixed(1)}" y="${y + 53}">${dtxt}</text>`; }
      }
      const sel = this._open === 'devices' && this._dev === d.device_id;
      const keyOf = k => k.split('|').includes(d.device_id) && /^(t|dd)\|/.test(k);
      const dstg = [...this._stg.keys()].some(keyOf);
      const dfail = [...this._pend.entries()].some(([k, p]) => keyOf(k) && p.status === 'failed');
      const dpend = [...this._pend.entries()].some(([k, p]) => keyOf(k) && p.status === 'pending');
      const hw = Math.max(48, Math.min(G.slotW / 2, Math.max(nw + (low ? 16 : 0), 42) / 2 + 6));
      const aria = `${d.name}, ${on ? 'online' : 'offline'}${low ? `, battery ${bat} %` : ''}. In ${this._mname(pm === 'schedule' ? hb.guard_mode : pm)}: ${on ? (act.map(f => f.label).join(', ') || 'no actions') : 'unknown'}. Edit its actions`;
      // the node's box hides other devices' links passing under it; its own link is drawn again inside it
      const box = `x="${(x - hw).toFixed(1)}" y="${y - 26}" width="${(2 * hw).toFixed(1)}" height="86" rx="12"`;
      nodes += `<g class="node${on ? '' : ' off'}${sel ? ' sel' : ''}" role="button" tabindex="0" data-node="${esc(d.device_id)}" data-focus="dev-${esc(d.device_id)}" aria-label="${esc(aria)}" aria-pressed="${sel}">
        <clipPath id="nc${i}"><rect ${box}/></clipPath><rect class="hit" ${box}/>
        <line class="link${on ? '' : ' off'}" clip-path="url(#nc${i})" x1="${x2.toFixed(1)}" y1="${y2.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}"/>${hubAlarm ? `<line class="flow" clip-path="url(#nc${i})" x1="${x2.toFixed(1)}" y1="${y2.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}"/>` : ''}
        <circle class="plate" cx="${x}" cy="${y}" r="21"/>${d.kind === 'sensor' ? senDraw(x, y) : camDraw(x, y)}
        ${dstg || dfail || dpend ? `<circle class="ndot${dfail ? ' err' : dpend ? ' pend' : ''}" cx="${x + 16}" cy="${y - 15}" r="4.5"/>` : ''}
        <text class="nname" x="${(x - (low ? 8 : 0)).toFixed(1)}" y="${y + 36}" text-anchor="middle">${esc(nm)}</text>
        ${low ? svgIcon('battery-20', x - 8 + nw / 2 + 9, y + 32, 13, 'bat') : ''}${row}</g>`;
    });
    const disk = hb.status && hb.status.storage && hb.status.storage.disk;
    const dtxt = disk && typeof disk.used_percent === 'number' ? `${Math.round(disk.used_percent)} %` : '';
    const stagedRing = ms ? `<circle class="sring m-${this._fam(ms.value)}" cx="${hx}" cy="${hy}" r="${HUB_R + 6}"/>` : '';
    const hub = `<g class="hub m-${fam}${trig ? ' alarm' : ''}" role="button" tabindex="0" data-hub data-focus="hub" aria-label="${esc(`${hb.name}, ${hb.model || 'HomeBase'}: ${this._mname(hb.guard_mode)}${dtxt ? `, storage ${dtxt} used` : ''}. Status`)}">
      <circle class="disc" cx="${hx}" cy="${hy}" r="${HUB_R}"/>${stagedRing}<circle class="ring" cx="${hx}" cy="${hy}" r="${HUB_R}"/>
      <rect class="hbody" x="${hx - 17}" y="${hy - 23}" width="34" height="46" rx="9"/>
      ${svgIcon(trig ? 'bell-ring' : this._micon(hb.guard_mode === 'schedule' ? (hb.effective_mode || 'home') : hb.guard_mode), hx, hy - 5, 18, 'hm')}
      <rect class="led" x="${hx - 9}" y="${hy + 12}" width="18" height="3" rx="1.5"/>
      ${dtxt ? `${svgIcon('harddisk', hx - textW(dtxt, txtFont) / 2 - 2, hy + HUB_R + 15, 12, 'hd')}<text class="ntxt" x="${(hx - textW(dtxt, txtFont) / 2 + 6).toFixed(1)}" y="${hy + HUB_R + 19}">${esc(dtxt)}</text>` : ''}</g>`;
    const svg = `<svg class="draw${sched ? ' dim' : ''}" width="${W}" height="${G.D}" viewBox="0 0 ${W} ${G.D}" role="group" aria-label="${esc(`${hb.name} and its devices${pages > 1 ? `, page ${this._page + 1} of ${pages}` : ''}`)}">${links}${hub}${nodes}</svg>`;
    const pager = pages > 1 ? `<div class="pager" role="group" aria-label="Device pages">
      <button type="button" class="pgb" data-pg="prev" data-focus="pg-prev" aria-label="Previous devices">${icon('chevron-left')}</button>
      ${Array.from({ length: pages }, (_, i) => `<button type="button" class="pgd${i === this._page ? ' on' : ''}" data-pg="${i}" data-focus="pg-${i}" aria-label="Devices page ${i + 1} of ${pages}"${i === this._page ? ' aria-current="true"' : ''}><i></i></button>`).join('')}
      <button type="button" class="pgb" data-pg="next" data-focus="pg-next" aria-label="Next devices">${icon('chevron-right')}</button></div>` : '';
    const lens = showLens ? this._lens(W, pm) : '';
    const schedHtml = sched ? this._schedule(G.D) : '';
    const confirm = this._confirm ? this._confirmBox() : '';
    const medit = this._medit && this._shows('custom_modes') ? this._meditBox() : '';
    const toast = this._toast && this._toast.until > Date.now() ? `<div class="toast" role="status" style="bottom:${(showLens ? LENS_H : 0) + 6}px">${icon('alert-circle-outline')}<span>${esc(this._toast.text)}</span></div>` : '';
    return `<div class="pic" style="height:${H}px">${svg}${schedHtml}${sched ? '' : pager}${lens}${toast}${medit}${confirm}</div>`;
  }
  _lens(W, pm) {
    const ids = this._lensIds(), hb = this._hb;
    const lab = id => (id === 'schedule' ? this._mname('schedule') : this._mname(id));
    const inForce = id => id !== 'schedule' && (hb.effective_mode || hb.guard_mode) === id && !['disarmed', 'off'].includes(id);
    const f12 = this._font(400, this._fs()), f12b = this._font(600, this._fs());
    const need = ids.reduce((s, id) => s + Math.max(textW(lab(id), f12), textW(lab(id), f12b)) + 24 + (inForce(id) ? 13 : 0) + (id === 'schedule' ? 20 : 0), 0) + 2 * (ids.length - 1) + 4;
    const btn = (id, tag = 'lens') => `<button type="button" role="radio" class="lo${id === pm ? ' on' : ''}" aria-checked="${id === pm}" data-${tag}="${esc(id)}" data-focus="lens-${esc(id)}" aria-label="${esc(`Show ${lab(id)}${inForce(id) ? ' (in force)' : ''}`)}">${inForce(id) ? `<i class="ldot m-${this._fam(id)}"></i>` : ''}${id === 'schedule' ? icon('calendar-clock') : ''}<span>${esc(lab(id))}</span></button>`;
    if (need <= W - 20) return `<div class="lens" role="radiogroup" aria-label="Show what the devices do in a mode">${ids.map(id => btn(id)).join('')}</div>`;
    return `<div class="lens step" role="group" aria-label="Show what the devices do in a mode">
      <button type="button" class="lo ls" data-lstep="-1" data-focus="lens-prev" aria-label="Previous mode">${icon('chevron-left')}</button>
      <span class="lo on" role="status" aria-label="${esc(`Showing ${lab(pm)}`)}">${inForce(pm) ? `<i class="ldot m-${this._fam(pm)}"></i>` : ''}${pm === 'schedule' ? icon('calendar-clock') : ''}<span>${esc(lab(pm))}</span></span>
      <button type="button" class="lo ls" data-lstep="1" data-focus="lens-next" aria-label="Next mode">${icon('chevron-right')}</button></div>`;
  }
  // Read-only week strip of the schedule (the staged one while staged; weekday 0 = Sunday), Monday first;
  // the default mode fills the gaps
  _schedule(D) {
    const S = this._schedVal(), H = this._hass;
    const days = [1, 2, 3, 4, 5, 6, 0];
    const dn = wd => dayName(H, wd);
    const used = [...new Set([S.default, ...(S.slots || []).map(s => s.mode)].filter(Boolean))];
    const col = id => (id ? this._scol(id) : 'var(--cc-track)');
    const seg = s => { const a = minutes(s.start), b = minutes(s.end, true); return `<i class="sg" style="background:${col(s.mode)};left:${(a / 14.4).toFixed(2)}%;width:${(Math.max(0, b - a) / 14.4).toFixed(2)}%"></i>`; };
    const rows = days.map(wd => `<div class="sr"><span class="sd">${esc(dn(wd))}</span><div class="sb" style="background:${col(S.default)}">${(S.slots || []).filter(s => s.weekday === wd).map(seg).join('')}</div></div>`).join('');
    const ticks = [0, 6, 12, 18].map(h => `<span style="left:${(h / 24) * 100}%">${esc(clockTime(H, new Date(2000, 0, 1, h, 0)))}</span>`).join('');
    const desc = days.map(wd => { const sl = (S.slots || []).filter(s => s.weekday === wd); return sl.length ? `${dn(wd)} ${sl.map(s => `${hhmm(H, s.start)}–${hhmm(H, s.end)} ${this._mname(s.mode)}`).join(', ')}` : ''; }).filter(Boolean).join('; ');
    return `<div class="sched" style="height:${D}px" role="img" aria-label="${esc(`Schedule: ${desc || 'no slots'}; otherwise ${this._mname(S.default)}`)}">
      <div class="sr st"><span class="sd"></span><div class="stk">${ticks}</div></div>${rows}
      <div class="slg">${used.map(m => `<span><i style="background:${col(m)}"></i>${esc(this._mname(m))}</span>`).join('')}</div></div>`;
  }
  // Custom modes over the picture: rename or delete each, add one; the library's rules answer as error text
  _meditBox() {
    const hb = this._hb, cus = hb.modes.filter(m => m.custom), can = this._can('custom_modes');
    const sub = (p, txt) => (p && p.status === 'failed' ? `Not applied${p.msg ? `: ${p.msg}` : ''}` : p ? 'sending' : txt);
    const rows = cus.map((m) => {
      const kr = `mc|ren|${m.id}`, kd = `mc|del|${m.id}`, sr = this._stg.get(kr), sd = this._stg.has(kd), p = this._pend.get(kd) || this._pend.get(kr);
      const iu = this._inuse[m.id] && this._inuse[m.id].rev === hb.revision ? this._inuse[m.id].reason : '';
      const why = m.id === hb.guard_mode || m.id === hb.effective_mode ? 'The current mode cannot be deleted' : iu;
      const line = sub(p, sd ? 'Deleted on Set' : why);
      return `<div class="mrow${sr || sd ? ' staged' : ''}${p && p.status === 'failed' ? ' failed' : ''}">
        <input class="mname" type="text" maxlength="${MODE_NAME_MAX}" data-mren="${esc(m.id)}" data-focus="m-name-${esc(m.id)}" value="${esc(sr ? sr.name : m.name)}" aria-label="${esc(`Name of ${m.name}`)}"${can && !sd ? '' : ' disabled'}>
        <button type="button" class="btn icon sm${sd ? ' primary' : ''}" data-mdel="${esc(m.id)}" data-focus="m-del-${esc(m.id)}" aria-pressed="${sd}" aria-label="${esc(`Delete ${m.name}${why ? `: ${why}` : ''}`)}"${can && (!why || sd) ? '' : ' disabled'}>${icon('delete')}</button>
        <span class="msub${p && p.status === 'failed' ? ' err' : ''}">${esc(line)}</span></div>`;
    }).join('');
    const sn = this._stg.get('mc|new'), pn = this._pend.get('mc|new');
    let add;
    if (sn || pn || this._mnew) {
      const nm = sn ? sn.name : pn && !hb.modes.some(m => m.custom && m.name === pn.entry.name) ? pn.entry.name : '';
      add = `<div class="mrow${sn ? ' staged' : ''}${pn && pn.status === 'failed' ? ' failed' : ''}"><input class="mname" type="text" maxlength="${MODE_NAME_MAX}" data-mnew data-focus="m-new" value="${esc(nm)}" placeholder="New mode" aria-label="Name of the new mode"${can && !(pn && pn.status === 'pending') ? '' : ' disabled'}>
        <span class="btn-ph"></span><span class="msub${pn && pn.status === 'failed' ? ' err' : ''}">${esc(sub(pn, sn ? 'Added on Set' : ''))}</span></div>`;
    } else if (cus.length >= CUSTOM_MAX) add = `<div class="mrow"><span class="msub full">${esc(`${CUSTOM_MAX} custom modes: delete one to add another`)}</span></div>`;
    else add = `<div class="mrow"><button type="button" class="madd" data-madd data-focus="m-add" aria-label="Add a custom mode"${can ? '' : ' disabled'}>${icon('plus')}<span>Mode</span></button></div>`;
    return `<div class="confirm medit" role="dialog" aria-label="Custom modes">
      <div class="cfh">${icon('shield-edit-outline')}<span>Custom modes</span><button type="button" class="btn icon sm mclose" data-act="m-close" data-focus="m-close" aria-label="Close the custom modes">${icon('close')}</button></div>
      <div class="cfl mlist">${rows}${add}</div>${this._note2([['custom_modes', 'custom modes']])}</div>`;
  }
  _confirmBox() {
    const lines = this._alarmEntries().map(x => this._effect(x));
    return `<div class="confirm" role="alertdialog" aria-label="These changes affect the alarm">
      <div class="cfh">${icon('shield-alert')}<span>Affects the alarm</span></div>
      <div class="cfl">${lines.map(([i, t]) => `<div class="cfi">${icon(i)}<span>${esc(t)}</span></div>`).join('')}</div>
      <div class="cfb"><button type="button" class="btn" data-act="cf-back" data-focus="cf-back" aria-label="Back to the changes">Back</button>
      <button type="button" class="btn primary warn" data-act="cf-set" data-focus="cf-set" aria-label="${esc(`Set now: ${lines.map(l => l[1]).join('; ')}`)}">Set</button></div></div>`;
  }

  _secs() {
    const out = [];
    if (this._devs().length && (this._shows('mode_tables') || this._shows('mode_delays')) && this._tmodes().length) out.push(['devices', 'cctv', 'Devices']);
    if (this._shows('schedule')) out.push(['schedule', 'calendar-clock', 'Schedule']);
    if (this._shows('arming_groups') && !(this._groups && this._groups.err === 'not_supported')) out.push(['groups', 'account-group', 'Groups']);
    if (this._setSecs().length) out.push(['settings', 'cog', 'Settings']);
    out.push(['status', 'information-outline', 'Status']);
    return out;
  }
  _setSecs() { return (this._lay || []).filter(s => s.entity_ids.some(e => this._st(e))); }
  _secOf(k) {
    if (/^(t|dv|dd)\|/.test(k)) return 'devices';
    if (/^(ga|gu|gd)\||^gn$/.test(k)) return 'groups';
    if (k.startsWith('e|') || k.startsWith('xc|')) return 'settings';
    if (k === 'sched') return 'schedule';
    return null;
  }
  // Toggle look by width: full (icon, name, chevron), name (icon, name), icon (icons; the open name in the section)
  _secFit(W, secs) {
    const per = (W - 24) / secs.length - 4;
    const f = this._font(600, 14);
    const lw = Math.max(...secs.map(s => textW(s[2], f)));
    if (lw + 18 + 6 + 16 + 6 + 12 <= per) return 'full';
    if (lw + 18 + 6 + 12 <= per) return 'name';
    return 'icon';
  }
  _toggles(W) {
    const secs = this._secs(); if (this._open && !secs.some(s => s[0] === this._open)) this._open = null;
    const fit = this._secFit(W, secs);
    const mark = id => { let m = ''; for (const [k, p] of this._pend) if (this._secOf(k) === id && p.status === 'failed') m = ' err'; if (!m) for (const k of this._stg.keys()) if (this._secOf(k) === id) m = ' stg'; return m; };
    return `<div class="secs fit-${fit}" role="tablist" aria-label="Sections">${secs.map(([id, ic, l]) => `<button type="button" role="tab" class="sec${this._open === id ? ' on' : ''}${mark(id)}" data-sec="${id}" data-focus="sec-${id}" aria-selected="${this._open === id}" aria-expanded="${this._open === id}" aria-label="${esc(`${l}${mark(id) === ' err' ? ', not applied' : mark(id) === ' stg' ? ', changed' : ''}`)}">${icon(ic)}<span>${esc(l)}</span>${icon('chevron-down', 'chev')}<i class="tdot" aria-hidden="true"></i></button>`).join('')}</div>`;
  }
  _section(W) {
    if (!this._open) return '';
    const name = (this._secs().find(s => s[0] === this._open) || [])[2];
    const head = this._secFit(W, this._secs()) === 'icon' ? `<div class="sname" aria-hidden="true">${esc(name)}</div>` : '';
    const body = this._open === 'devices' ? this._devices(W) : this._open === 'schedule' ? this._schedSec() : this._open === 'groups' ? this._groupsHtml() : this._open === 'settings' ? this._settings(W) : this._statusHtml();
    return `<div class="sect" role="tabpanel" aria-label="${esc(name)}">${head}${body}</div>`;
  }
  _note2(feats) {
    const d = feats.filter(([f]) => this._sup(f).grade === 'declared').map(([, l]) => l);
    return d.length ? `<div class="note" role="note">${icon('flask-outline')}<span>Not tested on this model${feats.length > 1 ? `: ${d.join(', ')}` : ''}</span></div>` : '';
  }

  // ----- Devices: one device's actions × modes; the tallest device kind sets the height -----
  _devices(W) {
    const devs = this._devs();
    if (!this._dev || !devs.some(d => d.device_id === this._dev)) this._pick(devs[0].device_id);
    const cur = devs.find(d => d.device_id === this._dev);
    const tm = this._tmodes(), pm0 = this._previewMode(), pm = pm0 === 'schedule' ? (tm[0] || {}).id : pm0;
    const showFlags = this._shows('mode_tables'), showDelays = this._shows('mode_delays');
    // every mode as a column when the names fit, else the previewed mode only
    const f12 = this._font(600, this._fs()), avail = Math.min(W, 560) - 24;
    const cw = m => Math.ceil(Math.max(46, textW(m.name, f12) + 16));
    const lblW = 132;
    const modes = lblW + tm.reduce((s, m) => s + cw(m), 0) <= avail ? tm : tm.filter(m => m.id === pm);
    const sig = d => `${showFlags ? this._flagsOf(d).map(f => f.id).join(',') : ''}|${showDelays}`;
    const sizers = [...new Map(devs.filter(d => d.device_id !== cur.device_id).map(d => [sig(d), d])).values()].filter(d => sig(d) !== sig(cur));
    const pane = (d, vis) => `<div class="pane narrowp"${vis ? '' : ' hidden-pane inert aria-hidden="true"'}>${this._devPick(d, vis, W)}${this._matrix(d, modes, pm, cw, vis, showFlags, showDelays)}${this._delaySlot(d, pm, vis, showDelays)}</div>`;
    return `<div class="stack">${pane(cur, true)}${sizers.map(d => pane(d, false)).join('')}</div>${this._note2([['mode_tables', 'actions'], ['mode_delays', 'delays']])}`;
  }
  _devPick(d, vis, W) {
    const on = this._online(d), bat = typeof d.battery === 'number' ? d.battery : NaN, open = vis && this._dd === 'devpick';
    // the model only when it fits beside the name and the chip (Status lists it too)
    const room = Math.min(W, 560) - 20 - 18 - 18 - 18 - 8 * 4 - textW(d.name, this._font(600, 14)) - (!on ? 62 : bat < 30 ? 64 : 0);
    const model = d.model && textW(d.model, this._font(400, this._fs())) <= room ? d.model : '';
    const devs = this._devs();
    const list = open ? this._ddList('devpick', 'Devices', devs.length, `${devs.map(x => `<button type="button" role="option" class="ddo${this._online(x) ? '' : ' off'}" data-ddo="${esc(x.device_id)}" data-focus="ddo-devpick-${esc(x.device_id)}" aria-selected="${x.device_id === d.device_id}" aria-label="${esc(`${x.name}${this._online(x) ? '' : ', offline'}`)}">${icon(this._kindIcon(x.kind))}<span>${esc(x.name)}</span>${x.device_id === d.device_id ? icon('check', 'chk') : this._online(x) ? '' : '<span class="later">Offline</span>'}</button>`).join('')}`) : '';
    return `<div class="dhead ddw"><button type="button" class="dpick" ${vis ? 'data-dd="devpick" data-focus="dd-devpick"' : 'tabindex="-1"'} aria-haspopup="listbox" aria-expanded="${open}" aria-label="${esc(`Device: ${d.name}, ${d.model || ''}${on ? '' : ', offline'}. Choose another device (${devs.length})`)}">${icon(this._kindIcon(d.kind))}<span class="dn">${esc(d.name)}</span>${model ? `<span class="dm">${esc(model)}</span>` : ''}${!on ? '<span class="chip off">Offline</span>' : bat < 30 ? `<span class="chip lo">${icon('battery-20')}${bat} %</span>` : ''}${icon(open ? 'chevron-up' : 'chevron-down', 'ddc')}</button>${list}</div>`;
  }
  _matrix(d, modes, pm, cw, vis, showFlags, showDelays) {
    const hb = this._hb, inForce = hb.effective_mode || hb.guard_mode;
    const flags = showFlags ? this._flagsOf(d) : [];
    const id = d.device_id, fx = vis ? '' : 'z-', canT = this._can('mode_tables');
    let g = `<div class="mx" role="grid" aria-label="${esc(`${d.name}: actions per mode`)}" style="grid-template-columns: minmax(0, 1fr) ${modes.map(m => `${cw(m)}px`).join(' ')}"><div class="mh c0" role="columnheader"></div>`;
    g += modes.map(m => `<button type="button" role="columnheader" class="mh${m.id === pm ? ' pv' : ''}" data-lens="${esc(m.id)}" data-focus="${fx}mh-${esc(m.id)}" aria-label="${esc(`Show ${m.name} on the drawing`)}"${vis ? '' : ' tabindex="-1"'}>${inForce === m.id && !['disarmed', 'off'].includes(m.id) ? `<i class="ldot m-${this._fam(m.id)}"></i>` : ''}<span>${esc(m.name)}</span></button>`).join('');
    for (const f of flags) {
      g += `<div class="ml c0" role="rowheader">${icon(FLAG_ICON[f.id] || 'alert-circle', this._alarmFlag(f) ? 'al' : '')}<span>${esc(f.label)}</span></div>`;
      g += modes.map((m) => {
        const v = this._flag(m.id, id, f.id), key = `t|${m.id}|${id}|${f.id}`, st = this._status(key);
        return `<div class="cellw${m.id === pm ? ' pv' : ''}"><button type="button" role="switch" class="cell${v ? ' on' : ''}${st ? ` ${st}` : ''}"${v === undefined || !canT ? ' disabled' : ''}${vis ? '' : ' tabindex="-1"'} data-cell="${esc(`${m.id}|${id}|${f.id}`)}" data-focus="${fx}c-${esc(key)}" aria-checked="${!!v}" aria-label="${esc(`${d.name}, ${m.name}: ${f.label}${v === undefined ? ', not reported' : ''}${canT ? '' : ', changing not available yet'}${st === 'failed' ? ', not applied' : st === 'pending' ? ', sending' : ''}`)}">${v === undefined ? '<span class="na">–</span>' : v ? icon('check') : ''}</button></div>`;
      }).join('');
    }
    if (showDelays) {
      for (const which of ['entry', 'leaving']) {
        const lab = which === 'entry' ? 'Entry delay' : 'Leaving delay';
        g += `<div class="ml c0 dly" role="rowheader">${icon(which === 'entry' ? 'timer-outline' : 'timer-sand')}<span>${lab}</span></div>`;
        g += modes.map((m) => {
          const v = this._devDelay(m.id, id, which), on = this._devDelayOn(m.id, id, which);
          const st = this._status(`dd|${m.id}|${which}|${id}`) || this._status(`dv|${m.id}|${which}`);
          const sel = this._dsel[id] || { mode: pm, which: 'entry' }, ch = sel.mode === m.id && sel.which === which;
          return `<div class="cellw${m.id === pm ? ' pv' : ''}"><button type="button" class="dcell${ch ? ' ch' : ''}${st ? ` ${st}` : ''}"${v === undefined ? ' disabled' : ''}${vis ? '' : ' tabindex="-1"'} data-dcell="${esc(`${m.id}|${which}`)}" data-dev="${esc(id)}" data-focus="${fx}d-${esc(`${id}-${which}-${m.id}`)}" aria-pressed="${ch}" aria-label="${esc(`${d.name}, ${m.name}: ${lab} ${v === undefined ? 'not reported' : on ? `${v} s` : 'off'}; adjust`)}">${v === undefined ? '–' : on ? `${esc(v)}<small>s</small>` : '<span class="na">Off</span>'}</button></div>`;
        }).join('');
      }
    }
    if (!flags.length && !showDelays) g += `<div class="ml c0 none" role="rowheader">${icon('alert-circle')}<span>No actions reported</span></div>`;
    return `${g}</div>`;
  }
  _delaySlot(d, pm, vis, showDelays) {
    if (!showDelays) return '<div class="row slot"></div>';
    const id = d.device_id, sel = this._dsel[id] || { mode: pm, which: 'entry' };
    const mode = this._tmodes().some(m => m.id === sel.mode) ? sel.mode : pm, which = sel.which;
    const on = this._devDelayOn(mode, id, which), mv = this._modeDelay(mode, which);
    const lab = `${which === 'entry' ? 'Entry delay' : 'Leaving delay'} · ${this._mname(mode)}`;
    if (on === undefined) return `<div class="row slot na"><div class="rname">${icon('timer-outline')}<span class="rn">${esc(lab)}</span></div><span class="rval">Not reported</span><div class="rctl"></div></div>`;
    const cur = mv || 0, steps = this._steps().length ? this._steps() : [cur];
    const idx = steps.reduce((b, s, i) => (Math.abs(s - cur) < Math.abs(steps[b] - cur) ? i : b), 0);
    const kv = `dv|${mode}|${which}`, kd = `dd|${mode}|${which}|${id}`;
    const st = this._status(kd) || this._status(kv);
    const sub = st === 'failed' ? '<span class="rsub err">Not applied</span>' : st === 'pending' ? '<span class="rsub">sending</span>' : '<span class="rsub">every device</span>';
    const fx = vis ? '' : 'z-', ti = vis ? '' : ' tabindex="-1"', dis = this._can('mode_delays') && this._steps().length ? '' : ' disabled';
    return `<div class="row slot${st ? ` ${st}` : ''}"><div class="rname">${icon(which === 'entry' ? 'timer-outline' : 'timer-sand')}<span class="rn">${esc(lab)}</span>${sub}</div>
      <span class="rval">${on ? `${esc(cur)} s` : 'Off'}</span>
      <div class="rctl"><button type="button" class="tg${on ? ' on' : ''}" role="switch" aria-checked="${!!on}" data-dsw="${esc(`${mode}|${which}|${id}`)}" data-focus="${fx}dsw-${esc(id)}"${ti}${dis} aria-label="${esc(`${d.name} uses the ${which === 'entry' ? 'entry' : 'leaving'} delay in ${this._mname(mode)}`)}"><i></i></button>
      <input class="ctl-range" type="range" min="0" max="${steps.length - 1}" step="1" value="${idx}" style="--pos:${(idx / Math.max(1, steps.length - 1)) * 100}%" data-steps="${esc(JSON.stringify(steps))}" data-unit="s" data-dmode="${esc(mode)}" data-which="${which}" data-focus="${fx}dslider-${esc(id)}"${ti}${dis} aria-valuetext="${esc(`${cur} s`)}" aria-label="${esc(`${which === 'entry' ? 'Entry' : 'Leaving'} delay in ${this._mname(mode)}, every device that uses it`)}"></div></div>`;
  }

  // ----- Groups: the list and the editor share one cell -----
  _groupsHtml() {
    const A = this._groups, gdis = this._can('arming_groups') ? '' : ' disabled';
    if (!A) return '<div class="msg">Loading groups</div>';
    if (A.err) return `<div class="msg">${A.err === 'not_supported' ? 'This HomeBase has no arming groups' : 'Groups did not load'}</div>`;
    const list = `<div class="pane narrowp"${this._gview ? ' hidden-pane inert aria-hidden="true"' : ''}><div class="glist">${A.groups.map((g) => {
      const ka = `ga|${g.group_id}`, kd = `gd|${g.group_id}`, ku = `gu|${g.group_id}`;
      const sa = this._stg.has(ka), sd = this._stg.has(kd), su = this._stg.get(ku);
      const pa = this._pend.get(ka) || this._pend.get(kd) || this._pend.get(ku);
      const name = su ? su.name : g.name;
      const members = su ? su.members.map(m => ({ ...m, name: (A.candidates.find(c => c.device_id === m.device_id) || {}).name, label: this._candLabel(m.device_id, m.mode) })) : g.members;
      const res = this._gres[g.group_id];
      const failedRes = res && res.filter(r => r.outcome === 'failed');
      const sub = sd ? 'Deleted on Set' : pa && pa.status === 'failed' ? `Not applied${failedRes && failedRes.length ? `: ${failedRes.map(r => r.name || 'a device').join(', ')}` : ''}` : pa ? 'sending' : sa ? 'Activates on Set' : members.map(m => `${m.name || 'a device'} → ${m.label || this._mname(m.mode)}`).join(', ');
      return `<div class="grow${sa || sd || su ? ' staged' : ''}${pa && pa.status === 'failed' ? ' failed' : ''}">
        <span class="gi2">${icon(g.preset === 'leaving' ? 'home-export-outline' : g.preset === 'staying' ? 'home-import-outline' : 'account-group')}</span>
        <div class="gtx"><span class="gn">${esc(name)}</span><span class="gs${pa && pa.status === 'failed' ? ' err' : ''}">${esc(sub)}</span></div>
        ${g.active && !sa ? `<span class="gact" role="img" aria-label="${esc(`${g.name} is active`)}">${icon('check')}</span>` : `<button type="button" class="btn sm${sa ? ' primary' : ''}"${gdis} data-gact="${esc(g.group_id)}" data-focus="g-act-${esc(g.group_id)}" aria-pressed="${sa}" aria-label="${esc(`Activate ${g.name}`)}">${icon('play')}<span>Activate</span></button>`}
        <button type="button" class="btn icon sm"${gdis} data-gedit="${esc(g.group_id)}" data-focus="g-edit-${esc(g.group_id)}" aria-label="${esc(`Edit ${g.name}`)}">${icon('pencil')}</button></div>`;
    }).join('')}
      ${A.groups.length < 20 ? `<button type="button" class="gnew${this._stg.has('gn') ? ' staged' : ''}"${gdis} data-gnew data-focus="g-new" aria-label="New group">${icon('plus')}<span>${this._stg.has('gn') ? esc(this._stg.get('gn').name || 'New group') : 'New group'}</span></button>` : ''}</div></div>`;
    const gid = this._gview;
    const dr = gid ? this._groupDraft() : { name: '', members: [] };
    const g = gid && gid !== 'new' ? this._group(gid) : null;
    const logs = g ? this._logs[g.group_id] : null;
    const last = logs && logs.length ? logs[0] : null;
    const seg = (c) => {
      const m = dr.members.find(x => x.device_id === c.device_id);
      const opts = [...c.modes, { id: '', label: 'Not in group' }];
      return `<div class="row"><div class="rname">${icon('shield-home')}<span class="rn">${esc(c.name)}</span></div><div class="rctl"><div class="seg" role="group" aria-label="${esc(`${c.name} mode in this group`)}">${opts.map(o => `<button type="button" class="opt txt" data-gmode="${esc(`${c.device_id}|${o.id}`)}" data-focus="gm-${esc(c.device_id)}-${esc(o.id || 'none')}" aria-pressed="${(m ? m.mode : '') === o.id}" aria-label="${esc(o.label)}"><span>${esc(o.id ? o.label : '–')}</span></button>`).join('')}</div></div></div>`;
    };
    const unknown = g ? g.members.filter(m => !m.device_id) : [];
    const edit = `<div class="pane narrowp"${gid ? '' : ' hidden-pane inert aria-hidden="true"'}><div class="gedit">
      <div class="ghd"><button type="button" class="btn icon sm" data-gback data-focus="g-back" aria-label="Back to the groups">${icon('arrow-left')}</button>
      <input class="gname" type="text" maxlength="30" data-gname data-focus="g-name" value="${esc(dr.name)}" placeholder="Name" aria-label="Group name">
      ${g ? `<button type="button" class="btn icon sm${this._stg.has(`gd|${g.group_id}`) ? ' primary' : ''}" data-gdel="${esc(g.group_id)}" data-focus="g-del" aria-pressed="${this._stg.has(`gd|${g.group_id}`)}" aria-label="${esc(`Delete ${g.name}`)}">${icon('delete')}</button>` : '<span class="btn-ph"></span>'}</div>
      ${A.candidates.map(seg).join('')}
      ${unknown.map(m => `<div class="row muted"><div class="rname">${icon('help-circle-outline')}<span class="rn">${esc(m.name || 'A device not in Home Assistant')}</span></div><span class="rval">${esc(m.label || '')}</span></div>`).join('')}
      <div class="glog">${icon('history')}<span>${last ? `${esc(last.user_name)} · ${esc(new Date(last.time).toLocaleDateString(this._hass.locale && this._hass.locale.language, { day: '2-digit', month: '2-digit' }))} ${esc(clockTime(this._hass, new Date(last.time)))}` : g ? (logs ? 'Not activated yet' : '…') : 'New'}</span></div>
    </div></div>`;
    return `<div class="stack">${list}${edit}</div>${this._note2([['arming_groups', 'groups']])}`;
  }
  _candLabel(dev, mode) { const c = this._groups.candidates.find(x => x.device_id === dev); const m = c && c.modes.find(x => x.id === mode); return m ? m.label : this._mname(mode); }

  // ----- Settings: tabs and headings from settings_layout only; every tab's pane in one cell -----
  _settings(W) {
    const secs = this._setSecs();
    const cur = secs.some(s => s.id === this._stab) ? this._stab : secs[0].id;
    const f12 = this._font(600, this._fs());
    const full = secs.reduce((s, x) => s + textW(x.heading, f12) + 16 + 4 + 16, 0) <= W - 20;
    const tabs = `<div class="stabs${full ? '' : ' fit1'}" role="tablist" aria-label="Settings">${secs.map((s) => {
      const xk = s.id === 'detection' && this._hb.cross_camera ? ['xc|cams', 'xc|groups'] : [];
      const stg = s.entity_ids.some(e => this._stg.has(`e|${e}`)) || xk.some(k => this._stg.has(k));
      const err = s.entity_ids.some(e => this._status(`e|${e}`) === 'failed') || xk.some(k => this._status(k) === 'failed');
      return `<button type="button" role="tab" class="stab${s.id === cur ? ' on' : ''}${err ? ' err' : stg ? ' stg' : ''}" data-stab="${esc(s.id)}" data-focus="stab-${esc(s.id)}" aria-selected="${s.id === cur}" tabindex="${s.id === cur ? 0 : -1}" aria-label="${esc(`${s.heading}${err ? ', not applied' : stg ? ', changed' : ''}`)}">${icon(SEC_ICON[s.id] || 'cog')}<span>${esc(s.heading)}</span><i class="tdot" aria-hidden="true"></i></button>`;
    }).join('')}<div class="stitle" aria-hidden="true">${esc((secs.find(s => s.id === cur) || {}).heading || '')}</div></div>`;
    const panes = secs.map((s) => {
      let lastHead = null, html = '', two = '', one = '';
      const vis = s.id === cur;
      for (const eid of s.entity_ids) {
        const st = this._st(eid); if (!st) continue;
        let name = this._ename(eid);
        const m = name.match(/^([^:]+):\s*(.+)$/);
        let head = '';
        if (m) { if (m[1] !== lastHead) head = `<div class="ghead">${esc(m[1])}</div>`; lastHead = m[1]; name = m[2]; } else lastHead = null;
        const r = this._setRow(eid, name, s, vis);
        if (head || r.tg) { html += two + one; two = ''; one = ''; }
        html += head; if (r.tg) one += r.html; else two += r.html;
      }
      html += two + one;
      const extra = s.id === 'detection' ? this._crossCam(vis) : '';
      return `<div class="pane"${vis ? '' : ' hidden-pane inert aria-hidden="true"'}><div class="ctl-rows">${html}</div>${extra}</div>`;
    }).join('');
    return `${tabs}<div class="stack">${panes}</div>`;
  }
  _ename(id) {
    const s = this._st(id), H = this._hass;
    const reg = H.entities && H.entities[id], dev = reg && H.devices && H.devices[reg.device_id];
    const dn = dev ? (dev.name_by_user || dev.name) : this._hb.name;
    const fn = (s && s.attributes.friendly_name) || id;
    return dn && fn.startsWith(`${dn} `) ? fn.slice(dn.length + 1) : fn;
  }
  _fmt(id, v) { const s = this._st(id); try { return s && this._hass.formatEntityState ? this._hass.formatEntityState(s, v) : v; } catch (e) { return v; } }
  _setRow(eid, name, sec, vis) {
    const st = this._st(eid), dom = eid.split('.')[0], key = `e|${eid}`, v = this._val(key, st.state), status = this._status(key);
    const unk = NA(v), na = st.state === 'unavailable', a = st.attributes;
    const ti = vis ? '' : ' tabindex="-1"', fx = vis ? '' : 'z-';
    const sub = status === 'failed' ? '<span class="rsub err">Not applied</span>' : status === 'pending' ? '<span class="rsub">sending</span>' : '';
    let ctl = '', val = unk && !na ? '<span class="rval unrep">Not reported</span>' : '';
    if (dom === 'switch') {
      ctl = `<button type="button" class="tg${v === 'on' ? ' on' : ''}${unk ? ' unk' : ''}" role="switch" aria-checked="${v === 'on'}" data-tg="${esc(eid)}" data-focus="${fx}tg-${esc(eid)}"${ti} aria-label="${esc(name)}"${na ? ' disabled' : ''}><i></i></button>`;
      return { tg: true, html: `<div class="row tgrow${status ? ` ${status}` : ''}${na ? ' na' : ''}"><div class="rname"><span class="rn">${esc(name)}</span>${sub}</div><span class="tgw">${val}${ctl}</span></div>` };
    }
    if (dom === 'number') {
      const mn = Number(a.min ?? 0), mx = Number(a.max ?? 100), stp = Number(a.step || 1);
      const pos = unk ? 0 : ((Number(v) - mn) / (mx - mn || 1)) * 100;
      val = unk ? val : `<span class="rval">${esc(v)}${a.unit_of_measurement ? ` ${esc(a.unit_of_measurement)}` : ''}</span>`;
      ctl = `<input class="ctl-range${unk ? ' unk' : ''}" type="range" min="${mn}" max="${mx}" step="${stp}" value="${unk ? mn : esc(v)}" style="--pos:${pos}%" data-ent="${esc(eid)}" data-unit="${esc(a.unit_of_measurement || '')}" data-focus="${fx}r-${esc(eid)}"${ti} aria-label="${esc(name)}"${na ? ' disabled' : ''}>`;
    } else if (dom === 'select') {
      const opts = a.options || [];
      const f12 = this._font(400, this._fs());
      if (opts.length <= 3 && opts.every(o => textW(this._fmt(eid, o), f12) <= 74)) {
        ctl = `<div class="seg" role="group" aria-label="${esc(name)}">${opts.map(o => `<button type="button" class="opt txt" data-opt="${esc(o)}" data-ent="${esc(eid)}" data-focus="${fx}o-${esc(eid)}-${esc(o)}"${ti} aria-pressed="${v === o}" aria-label="${esc(`${name}: ${this._fmt(eid, o)}`)}"${na ? ' disabled' : ''}><span>${esc(this._fmt(eid, o))}</span></button>`).join('')}</div>`;
        if (!unk) val = '';
      } else {
        const k = `sel|${eid}`, open = vis && this._dd === k;
        const list = open ? this._ddList(k, name, opts.length, opts.map((o, i) => `<button type="button" role="option" class="ddo" data-ddo="${esc(o)}" data-focus="ddo-${esc(k)}-${i}" aria-selected="${o === v}" aria-label="${esc(this._fmt(eid, o))}"><span>${esc(this._fmt(eid, o))}</span>${o === v ? icon('check', 'chk') : ''}</button>`).join('')) : '';
        ctl = `<div class="ddw"><button type="button" class="dd" data-dd="${esc(k)}" data-focus="${fx}dd-${esc(k)}"${ti} aria-haspopup="listbox" aria-expanded="${open}" aria-label="${esc(`${name}: ${unk ? 'not reported' : this._fmt(eid, v)}`)}"${na ? ' disabled' : ''}><span>${esc(unk ? 'Not reported' : this._fmt(eid, v))}</span>${icon(open ? 'chevron-up' : 'chevron-down')}</button>${list}</div>`;
        val = '';
      }
    }
    return { tg: false, html: `<div class="row${status ? ` ${status}` : ''}${na ? ' na' : ''}"><div class="rname"><span class="rn">${esc(name)}</span>${sub}</div>${val}<div class="rctl">${ctl}</div></div>` };
  }
  // Cross-camera tracking (Settings › Detection): the cameras (All = every camera) and the tracking groups, staged
  _crossCam(vis) {
    const c = this._hb.cross_camera;
    if (!c || !this._shows('cross_camera')) return '';
    const L = this._xcLive(), cams = this._xcCams(), ti = vis ? '' : ' tabindex="-1"', fx = vis ? '' : 'z-';
    const cv = this._val('xc|cams', L.cams), canC = this._can('cross_camera');
    const min = this._xcMin(), ids = cams.map(d => d.device_id), base = cv === null ? ids : cv.filter(x => ids.includes(x));
    const chip = (attr, key, on, label, dis, aria, ic) => `<button type="button" class="xchip" ${attr} data-focus="${fx}${esc(key)}"${ti} aria-pressed="${on}" aria-label="${esc(aria || label)}"${dis ? ' disabled' : ''}>${icon(on ? 'check' : ic || 'cctv')}<span>${esc(label)}</span></button>`;
    const allNo = this._xcShort(cv === null ? ids : null);
    // a chip that would leave fewer than the minimum cannot change the choice
    const camChips = [chip('data-xall', 'xc-all', cv === null, 'All cameras', !canC || allNo, `All cameras${allNo ? `: tracking needs at least ${min}` : ''}`),
      ...cams.map((d) => {
        const on = base.includes(d.device_id), lock = this._xcShort(on ? base.filter(x => x !== d.device_id) : [...base, d.device_id]);
        return chip(`data-xcam="${esc(d.device_id)}"`, `xc-${d.device_id}`, on, d.name, !canC || lock, `${d.name}${lock ? `: tracking needs at least ${min} cameras` : ''}`, this._kindIcon(d.kind));
      })].join('');
    // status: a write's outcome, else a staged choice Set refuses, else why chips are locked
    const blk = this._setBlock(), short = ids.length < min;
    const stat = (keys) => { const p = keys.map(k => this._pend.get(k)).find(Boolean); return p && p.status === 'failed' ? ['err', `Not applied${p.msg ? `: ${p.msg}` : ''}`] : p ? ['', 'sending'] : blk ? ['warn', blk] : short ? ['', `Tracking needs ${min} cameras; ${ids.length} can be tracked`] : canC && base.length <= min ? ['', `At least ${min} cameras`] : ['', '']; };
    let groups = '';
    if (this._shows('cross_camera_groups')) {
      const gv = this._val('xc|groups', L.groups), canG = this._can('cross_camera_groups');
      const rows = gv.map((g, gi) => `<div class="xgrow"><span class="xgn" aria-hidden="true">${gi + 1}</span><div class="xgm" role="group" aria-label="${esc(`Tracking group ${gi + 1}`)}">${cams.map(d => `<button type="button" class="xchip sm" data-xg="${esc(`${gi}|${d.device_id}`)}" data-focus="${fx}xg-${gi}-${esc(d.device_id)}"${ti} aria-pressed="${g.includes(d.device_id)}" aria-label="${esc(`Group ${gi + 1}: ${d.name}`)}"${canG ? '' : ' disabled'}><span>${esc(d.name)}</span></button>`).join('')}${g.filter(x => !x).map(() => '<span class="xchip sm unk">Unknown camera</span>').join('')}</div>
        <button type="button" class="btn icon sm" data-xgdel="${gi}" data-focus="${fx}xg-del-${gi}"${ti} aria-label="${esc(`Remove tracking group ${gi + 1}`)}"${canG ? '' : ' disabled'}>${icon('delete')}</button></div>`).join('');
      groups = `<div class="xh">Tracking groups</div><div class="xgl">${rows || '<div class="xnone">No groups</div>'}</div>
        <button type="button" class="gnew sm" data-xgadd data-focus="${fx}xg-add"${ti} aria-label="Add a tracking group"${canG ? '' : ' disabled'}>${icon('plus')}<span>Group</span></button>`;
    }
    const [sc, st] = stat(['xc|cams', 'xc|groups']);
    const time = typeof c.continuous_time_s === 'number' ? `<span class="xtime">${icon('timer-outline')}<span>${esc(`Tracking time ${dur(c.continuous_time_s)}`)}</span></span>` : '';
    return `<div class="xcam"><div class="xh">Cameras to track</div><div class="chips" role="group" aria-label="Cross-camera tracking cameras">${camChips}</div>
      ${groups}<div class="xfoot">${time}<span class="xst${sc ? ` ${sc}` : ''}" role="status">${esc(st)}</span></div>
      ${this._note2([['cross_camera', 'cameras'], ['cross_camera_groups', 'groups']])}</div>`;
  }

  // ----- Schedule: the week, the default mode, one slot form under the week (staged as one whole schedule) -----
  _schedSec() {
    const H = this._hass, v = this._schedVal(), can = this._can('schedule');
    if (this._sform && this._sform.idx !== null && !v.slots[this._sform.idx]) this._sform = { ...this._sform, idx: null };
    const f = this._sf();
    const days = [1, 2, 3, 4, 5, 6, 0], sm = this._schedModes();
    const p = this._pend.get('sched'), staged = this._stg.has('sched');
    const ticks = [0, 6, 12, 18].map(h => `<span style="left:${(h / 24) * 100}%">${esc(clockTime(H, new Date(2000, 0, 1, h, 0)))}</span>`).join('');
    const rows = days.map(wd => `<div class="sr"><span class="sd">${esc(dayName(H, wd))}</span><div class="sb" style="background:${v.default ? this._scol(v.default) : 'var(--cc-track)'}">${v.slots.map((x, i) => {
      if (x.weekday !== wd) return '';
      const a = minutes(x.start), b = minutes(x.end, true);
      return `<button type="button" tabindex="-1" class="sg${i === f.idx ? ' sel' : ''}" data-slot="${i}" style="background:${this._scol(x.mode)};left:${(a / 14.4).toFixed(2)}%;width:${(Math.max(0, b - a) / 14.4).toFixed(2)}%" aria-label="${esc(`${this._slotText(x)}: edit`)}"></button>`;
    }).join('')}</div></div>`).join('');
    const legend = sm.map(m => `<span><i style="background:${this._scol(m.id)}"></i>${esc(m.name)}</span>`).join('');
    const order = v.slots.map((x, i) => [x, i]).sort((a, b) => ((a[0].weekday + 6) % 7) - ((b[0].weekday + 6) % 7) || minutes(a[0].start) - minutes(b[0].start));
    const pick = this._ddHtml('slotpick', f.idx === null ? 'New slot' : this._slotText(v.slots[f.idx]), [['new', 'New slot'], ...order.map(([x, i]) => [String(i), this._slotText(x)])], f.idx === null ? 'new' : String(f.idx), 'Slot', !can);
    const tset = new Set(); for (let t = 0; t < 1440; t += SLOT_STEP) tset.add(hm(t));
    const tOpts = (extra, end) => { const l = new Set([...tset, extra]); if (end) { l.delete('00:00'); l.add(DAY_END); } return [...l].sort((a, b) => minutes(a) - minutes(b)).map(t => [t, hhmm(H, t)]); };
    const dayBtns = days.map(wd => `<button type="button" class="opt txt" data-sday="${wd}" data-focus="sday-${wd}" aria-pressed="${f.days.includes(wd)}" aria-label="${esc(new Date(2024, 0, 7 + wd).toLocaleDateString(H.locale && H.locale.language, { weekday: 'long' }))}"${can ? '' : ' disabled'}><span>${esc(dayName(H, wd))}</span></button>`).join('');
    const why = this._sformCheck(v, f);
    const msg = p && p.status === 'failed' ? ['err', `Not applied${p.msg ? `: ${p.msg}` : ''}`] : p ? ['', 'sending'] : why && can ? ['warn', why] : staged ? ['', 'The schedule changes on Set'] : ['', ''];
    const def = this._ddHtml('sf|def', v.default ? this._mname(v.default) : 'Not set', sm.map(m => [m.id, m.name]), v.default, 'Outside the slots', !can);
    return `<div class="schw${staged ? ' staged' : ''}${p && p.status === 'failed' ? ' failed' : ''}">
      <div class="sdef"><span class="slab">${icon('calendar-clock')}<span>Outside the slots</span></span>${def}</div>
      <div class="swk" role="group" aria-label="${esc(`Week: ${order.map(([x]) => this._slotText(x)).join('; ') || 'no slots'}`)}"><div class="sr st"><span class="sd"></span><div class="stk">${ticks}</div></div>${rows}</div>
      <div class="slg">${legend}</div>
      <div class="sfrm" role="group" aria-label="Slot">
        ${pick}
        <div class="seg sdays" role="group" aria-label="Days">${dayBtns}</div>
        <div class="stimes"><div class="sfl"><span>Start</span>${this._ddHtml('sf|start', hhmm(H, f.start), tOpts(f.start), f.start, 'Start', !can)}</div>
          <div class="sfl"><span>End</span>${this._ddHtml('sf|end', hhmm(H, f.end), tOpts(f.end, true), f.end, 'End', !can)}</div>
          <div class="sfl smode"><span>Mode</span>${this._ddHtml('sf|mode', this._mname(f.mode), sm.map(m => [m.id, m.name]), f.mode, 'Mode', !can)}</div></div>
        <div class="sbar"><span class="smsg${msg[0] ? ` ${msg[0]}` : ''}" role="status">${esc(msg[1])}</span>
          <button type="button" class="btn sm" data-sact="del" data-focus="sf-del" aria-label="Delete this slot"${can && f.idx !== null ? '' : ' disabled'}>${icon('delete')}<span>Delete</span></button>
          <button type="button" class="btn sm primary" data-sact="${f.idx === null ? 'add' : 'save'}" data-focus="sf-add" aria-label="${esc(f.idx === null ? 'Add the slot' : 'Save the slot')}${why ? `: ${esc(why)}` : ''}"${can && !why ? '' : ' disabled'}>${icon(f.idx === null ? 'plus' : 'check')}<span>${f.idx === null ? 'Add' : 'Save'}</span></button></div>
      </div></div>${this._note2([['schedule', 'schedule']])}`;
  }
  // An open dropdown list: a filter field above a long list, the options (labels ellipsize; aria-label has the full text)
  _ddList(k, aria, n, options) {
    const f = n > DD_FILTER ? `<input class="ddf" type="text" data-ddf data-focus="ddf-${esc(k)}" value="${esc(this._ddq)}" placeholder="Filter" autocomplete="off" spellcheck="false" aria-label="${esc(`Filter ${aria}`)}">` : '';
    return `<div class="ddl${f ? ' filt' : ''}">${f}<div class="ddi" role="listbox" aria-label="${esc(aria)}">${options}<div class="ddn" hidden>No matches</div></div></div>`;
  }
  // An in-card dropdown: opts [[value, text]]
  _ddHtml(k, text, opts, cur, aria, dis) {
    const open = this._dd === k;
    const list = open ? this._ddList(k, aria, opts.length, opts.map(([v, t], i) => `<button type="button" role="option" class="ddo" data-ddo="${esc(v)}" data-focus="ddo-${esc(k)}-${i}" aria-selected="${v === cur}" aria-label="${esc(t)}"><span>${esc(t)}</span>${v === cur ? icon('check', 'chk') : ''}</button>`).join('')) : '';
    return `<div class="ddw"><button type="button" class="dd" data-dd="${esc(k)}" data-focus="dd-${esc(k)}" aria-haspopup="listbox" aria-expanded="${open}" aria-label="${esc(`${aria}: ${text}`)}"${dis ? ' disabled' : ''}><span>${esc(text)}</span>${icon(open ? 'chevron-up' : 'chevron-down')}</button>${list}</div>`;
  }

  // ----- Status: storage, firmware, the last alarm, the paired devices -----
  _statusHtml() {
    const hb = this._hb, s = hb.status || {}, sto = s.storage || {};
    const bar = (ic, label, x) => {
      if (!x || typeof x.used_gib !== 'number') return '';
      const pct = typeof x.used_percent === 'number' ? x.used_percent : x.size_gib ? (x.used_gib / x.size_gib) * 100 : 0;
      const w = Math.max(1, Math.min(100, pct));
      return `<div class="srow"><div class="rname">${icon(ic)}<span class="rn">${esc(label)}</span></div><span class="sv">${esc(`${Math.round(x.used_gib * 10) / 10} of ${x.size_gib != null ? Math.round(x.size_gib * 10) / 10 : '?'} GiB · ${Math.round(pct)} %`)}</span><div class="bar" role="img" aria-label="${esc(`${label}: ${Math.round(pct)} % used`)}"><i style="width:${w}%"></i></div></div>`;
    };
    const up = this._st(s.update), upOn = up && up.state === 'on';
    const fw = s.firmware ? `<span>${icon('chip')}<span>${esc(`${hb.model || 'HomeBase'} · ${s.firmware}`)}</span></span>` : '';
    const upd = upOn ? `<button type="button" class="link" data-more="${esc(s.update)}" data-focus="upd" aria-label="${esc(`Update available: ${up.attributes.latest_version || ''}. Open`)}">${icon('update')}<span>${esc(`Update ${up.attributes.latest_version || 'available'}`)}</span></button>` : '';
    // the last alarm transition (its start while the alarm sounds): the alarm push's own time, else when the integration saw it
    const la = (hb.alarm && hb.alarm.last) || null, lt = la ? new Date(la.event_time || la.at) : null;
    const lang = this._hass.locale && this._hass.locale.language;
    const last = lt && !isNaN(lt) ? `<span class="slast">${icon('bell-ring')}<span>${esc(`${this._triggered() ? 'Alarm since' : 'Last alarm'} ${lt.toLocaleDateString(lang, { day: '2-digit', month: '2-digit' })} ${clockTime(this._hass, lt)}`)}</span></span>` : '';
    const devs = this._devs().map((d) => {
      const on = this._online(d), b = typeof d.battery === 'number' ? d.battery : null;
      const bi = b == null ? '' : `<span class="bat ${b >= 70 ? 'hi' : b >= 30 ? 'mid' : 'lo'}" role="img" aria-label="${esc(`Battery ${b} %`)}">${icon(b >= 95 ? 'battery' : `battery-${Math.max(10, Math.round(b / 10) * 10)}`)}<span>${b} %</span></span>`;
      return `<div class="drow2${on ? '' : ' off'}">${icon(this._kindIcon(d.kind), 'ki')}<div class="dn"><span class="rn">${esc(d.name)}</span><span class="dsub">${esc(d.model || '')}</span></div>${on ? bi : `<span class="offw">${icon('wifi-off')}<span>Offline</span></span>`}</div>`;
    }).join('');
    return `<div class="stl">${bar('harddisk', 'Disk', sto.disk)}${bar('memory', 'Internal storage', sto.emmc)}
      <div class="sline">${fw}${upd}${last}</div><div class="dlist">${devs}</div></div>`;
  }
}

function camDraw(x, y) {
  return `<g class="dev" transform="translate(${x} ${y})"><rect class="db" x="-12.5" y="-8.5" width="25" height="16" rx="5"/>
    <circle class="lr" cx="-2" cy="-0.5" r="5.6"/><circle class="ln" cx="-2" cy="-0.5" r="3.4"/><circle class="lh" cx="-3.1" cy="-1.6" r="1"/>
    <circle class="pir" cx="7" cy="-0.5" r="2"/><rect class="mt" x="-2" y="7.5" width="4" height="5" rx="1"/></g>`;
}
function senDraw(x, y) {
  return `<g class="dev" transform="translate(${x} ${y})"><rect class="db" x="-8.5" y="-11" width="17" height="22" rx="5"/>
    <path class="dome" d="M -5.6 -2.5 A 5.6 5.6 0 0 1 5.6 -2.5 Z"/><circle class="pir" cx="0" cy="5.5" r="1.3"/></g>`;
}
// Stations of the integration: devices with an alarm panel entity
function stations(hass) {
  const ents = (hass && hass.entities) || {};
  return [...new Set(Object.values(ents).filter(e => e.platform === DOMAIN && e.entity_id.startsWith('alarm_control_panel.') && e.device_id).map(e => e.device_id))];
}

const CSS = `
  :host {
    display: block;
    --cc-bg: var(--ha-card-background, var(--card-background-color, #fff));
    --cc-text: var(--primary-text-color, #212121);
    --cc-dim: var(--secondary-text-color, #727272);
    --cc-sub: color-mix(in srgb, var(--cc-dim) 70%, var(--cc-text));
    --cc-accent: var(--primary-color, #03a9f4);
    --cc-accent-text: color-mix(in srgb, var(--cc-accent) 45%, var(--cc-text));
    --cc-error: var(--error-color, #db4437);
    --cc-error-text: color-mix(in srgb, var(--cc-error) 70%, var(--cc-text));
    --cc-warn: var(--warning-color, #ffa600);
    --cc-warn-text: color-mix(in srgb, var(--cc-warn) 45%, var(--cc-text));
    --cc-on-accent: var(--text-primary-color, #fff);
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
    --cc-bat-lo: var(--state-sensor-battery-low-color, var(--error-color, #db4437));
    --cc-bat-mid: var(--state-sensor-battery-medium-color, var(--warning-color, #ffa600));
    --cc-bat-hi: var(--state-sensor-battery-high-color, var(--success-color, #43a047));
    /* security mode colours: HA's alarm panel state tokens */
    --m-away: var(--state-alarm_control_panel-armed_away-color, var(--green-color, #4caf50));
    --m-home: var(--state-alarm_control_panel-armed_home-color, var(--green-color, #4caf50));
    --m-custom: var(--state-alarm_control_panel-armed_custom_bypass-color, var(--green-color, #4caf50));
    --m-disarmed: var(--state-alarm_control_panel-disarmed-color, var(--state-inactive-color, #8a8a8a));
    --m-triggered: var(--state-alarm_control_panel-triggered-color, var(--red-color, #f44336));
    --m-pending: var(--state-alarm_control_panel-pending-color, var(--orange-color, #ff9800));
    --hb-alarm: color-mix(in srgb, var(--red-color, #f44336) 72%, var(--cc-text));
    --hb-pic: color-mix(in srgb, var(--cc-text) 3%, var(--cc-bg));
    --hb-ink: color-mix(in srgb, var(--cc-text) 62%, var(--cc-bg));
    --hb-line: color-mix(in srgb, var(--cc-text) 20%, transparent);
    --fs-xs: var(--ha-font-size-s, 12px);
    --fs-sm: var(--ha-font-size-m, 14px);
    --fs-md: var(--ha-font-size-l, 16px);
  }
  ha-card { overflow: hidden; }
  .cc { position: relative; color: var(--cc-text); }
  button { font: inherit; -webkit-tap-highlight-color: transparent; }
  .m-away { --mc: var(--m-away); } .m-home { --mc: var(--m-home); } .m-custom { --mc: var(--m-custom); }
  .m-disarmed { --mc: var(--m-disarmed); } .m-triggered { --mc: var(--m-triggered); } .m-pending { --mc: var(--m-pending); }

  /* Header: the camera card's */
  .hdr { padding: 12px 16px 10px; }
  .tline { position: relative; display: flex; align-items: center; min-height: 40px; min-width: 0; }
  .title { flex: 1 1 auto; display: flex; align-items: center; gap: 12px; min-width: 0; }
  .bwrap { position: relative; flex: none; }
  .htext { display: grid; min-width: 0; }
  .name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-md); font-weight: 500; line-height: 1.25; }
  .hsub { display: flex; flex-wrap: wrap; align-items: baseline; min-width: 0; height: 1.3em; overflow: hidden;
    font-size: var(--fs-sm); line-height: 1.3; color: var(--cc-sub); font-variant-numeric: tabular-nums; }
  .hsub .g { white-space: nowrap; }
  .hsub .sep { font-style: normal; opacity: 0.8; padding: 0 5px; }
  .hsub .st { color: var(--cc-text); font-weight: 500; }
  .hsub .st.staged { color: var(--cc-accent-text); font-weight: 600; }
  .hsub .st.pending { color: var(--cc-accent-text); font-style: italic; }
  .hsub .st.err, .hsub .err { color: var(--cc-error-text); font-weight: 600; }
  .hsub .warn { color: var(--cc-warn-text); font-weight: 600; }
  .hsub .offn { color: var(--cc-error-text); }
  .acts { position: absolute; right: 0; top: 50%; transform: translateY(-50%); display: flex; gap: 6px; padding-left: 10px;
    background: linear-gradient(90deg, transparent, var(--cc-bg) 10px); }
  .acts.off { visibility: hidden; }
  .hdr.tight .name { visibility: hidden; }
  .staging .title { padding-right: 118px; }
  .trig .title { padding-right: 104px; }
  .trig.staging .title { padding-right: 218px; }
  .trig.stop .title { padding-right: 142px; }
  .trig.stop.staging .title { padding-right: 256px; }
  .trig.stop.staging.dicon .title { padding-right: 196px; }
  .trig.stopw .title { padding-right: 226px; }
  .trig.stopw.staging .title { padding-right: 340px; }
  .btn { min-height: 32px; padding: 0 14px; border-radius: 16px; cursor: pointer; border: 1px solid color-mix(in srgb, var(--cc-text) 16%, transparent);
    background: transparent; color: var(--cc-text); font-size: var(--fs-sm); font-weight: 500; display: inline-flex; align-items: center; gap: 6px; }
  .btn:hover { background: color-mix(in srgb, var(--cc-text) 8%, transparent); }
  .btn:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 2px; }
  .btn.primary { border-color: transparent; background: var(--cc-sel); color: var(--cc-accent-text); }
  .btn.primary.warn { box-shadow: inset 0 0 0 1.5px var(--cc-warn); }
  .btn.primary:disabled { background: var(--cc-track); color: var(--cc-sub); box-shadow: none; cursor: default; }
  .btn.alarm { border-color: transparent; background: var(--m-triggered); color: var(--cc-on-accent); font-weight: 600; padding: 0 12px; }
  .btn.icon { width: 32px; padding: 0; justify-content: center; }
  .btn ha-icon { display: flex; line-height: 0; --mdc-icon-size: 18px; }
  .btn .cnt { display: inline-block; min-width: 16px; padding: 0 4px; border-radius: 8px; box-sizing: border-box;
    font-size: var(--fs-xs); line-height: 16px; text-align: center; background: color-mix(in srgb, var(--cc-accent) 30%, var(--cc-bg)); }
  .btn .cnt.none { visibility: hidden; }
  .mbtn { position: relative; width: 40px; height: 40px; border-radius: 50%; border: 0; padding: 0; margin: 0; cursor: pointer;
    display: grid; place-items: center; --mdc-icon-size: 22px; color: var(--mc); background: color-mix(in srgb, var(--mc) 20%, transparent); }
  .mbtn > ha-icon { color: inherit; display: flex; line-height: 0; }
  .mbtn:hover { background: color-mix(in srgb, var(--mc) 30%, transparent); }
  .mbtn:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 2px; }
  .mbtn.alarm { background: color-mix(in srgb, var(--mc) 38%, transparent); color: color-mix(in srgb, var(--mc) 80%, var(--cc-on-accent)); box-shadow: 0 0 10px color-mix(in srgb, var(--mc) 45%, transparent); }
  .mbtn.staged { box-shadow: 0 0 0 2px var(--cc-accent); }
  .mbtn.failed { box-shadow: 0 0 0 2px var(--cc-error); }
  .badge { position: absolute; top: -3px; right: -3px; width: 18px; height: 18px; border-radius: 50%; display: grid; place-items: center;
    background: var(--cc-sub); box-shadow: 0 0 0 1.5px var(--cc-bg); }
  .badge ha-icon { display: flex; width: 12px; height: 12px; line-height: 0; --mdc-icon-size: 12px; color: var(--cc-bg); }
  .menu { position: absolute; top: calc(100% + 4px); left: 0; z-index: 6; min-width: 220px; max-width: calc(100cqw - 32px); padding: 4px; border-radius: 12px; overflow-y: auto;
    background: var(--cc-bg); box-shadow: 0 4px 16px var(--cc-shadow); border: 1px solid var(--cc-line); display: grid; gap: 2px; box-sizing: border-box; }
  .cc { container-type: inline-size; }
  .mi { display: flex; align-items: center; gap: 10px; min-height: 36px; padding: 0 10px; border: 0; border-radius: 8px; cursor: pointer;
    background: transparent; color: var(--cc-text); font-size: var(--fs-sm); text-align: left; --mdc-icon-size: 18px; white-space: nowrap; }
  .mi ha-icon { color: var(--cc-sub); display: flex; line-height: 0; }
  .mi .chk { margin-left: auto; color: var(--cc-accent-text); }
  .mi .later { margin-left: auto; padding-left: 12px; font-size: var(--fs-xs); color: var(--cc-sub); }
  .mi[aria-disabled=true] { color: var(--cc-sub); cursor: default; }
  .mi.sep { margin-top: 3px; box-shadow: 0 -1px 0 var(--cc-line); border-top-left-radius: 0; border-top-right-radius: 0; }
  .mi:hover, .mi:focus-visible { background: color-mix(in srgb, var(--cc-text) 8%, transparent); outline: none; }
  .mi.sel { font-weight: 600; background: var(--cc-sel-quiet); }
  .mnote { padding: 0 10px 4px 38px; font-size: var(--fs-xs); color: var(--cc-sub); }

  /* Picture: the drawing (fixed height) with the mode lens along its bottom */
  .pic { position: relative; overflow: hidden; background: var(--hb-pic); }
  .pic.shell { display: grid; place-items: center; }
  .draw { display: block; position: absolute; left: 0; top: 0; overflow: visible; font-family: inherit; }
  .draw.dim { opacity: 0; }
  .draw text { font-size: var(--fs-xs); paint-order: stroke; stroke: var(--hb-pic); stroke-width: 3px; stroke-linejoin: round; }
  .link { stroke: var(--hb-line); stroke-width: 1.5; }
  .link.off { stroke-dasharray: 3 4; opacity: 0.6; }
  .flow { stroke: var(--hb-alarm); stroke-width: 2; stroke-linecap: round; stroke-dasharray: 1.5 9; animation: flow 2.6s linear infinite; }
  @keyframes flow { to { stroke-dashoffset: -21; } }
  .hub { cursor: pointer; outline: none; }
  .hub .disc { fill: color-mix(in srgb, var(--mc) 11%, var(--hb-pic)); }
  .hub .ring { fill: none; stroke: var(--mc); stroke-width: 3.5; }
  .hub.alarm .ring { animation: breathe 2.4s ease-in-out infinite alternate; }
  .hub.alarm .disc { fill: color-mix(in srgb, var(--mc) 18%, var(--hb-pic)); }
  @keyframes breathe { from { stroke-opacity: 1; stroke-width: 3.5; } to { stroke-opacity: 0.35; stroke-width: 6; } }
  .hub .sring { fill: none; stroke: var(--cc-accent); stroke-width: 2; stroke-dasharray: 4 4; }
  .hub .hbody { fill: var(--cc-bg); stroke: var(--hb-ink); stroke-width: 1.3; }
  .hub .led { fill: var(--mc); }
  .hub .hm { fill: color-mix(in srgb, var(--mc) 75%, var(--cc-text)); }
  .hub .hd { fill: var(--cc-sub); }
  .hub:focus-visible .ring { stroke: var(--cc-accent); }
  .node { cursor: pointer; outline: none; }
  .node .hit { fill: var(--hb-pic); }
  .node:hover .hit { fill: color-mix(in srgb, var(--cc-text) 4%, var(--hb-pic)); }
  .node:focus-visible .hit { stroke: var(--cc-accent); stroke-width: 2; }
  .node .plate { fill: var(--cc-bg); stroke: var(--hb-line); stroke-width: 1.3; }
  .node.sel .plate { stroke: var(--cc-accent); stroke-width: 2.5; }
  .node.off .plate { stroke-dasharray: 3 3; }
  .dev .db { fill: color-mix(in srgb, var(--cc-text) 7%, var(--cc-bg)); stroke: var(--hb-ink); stroke-width: 1.2; }
  .dev .lr { fill: var(--hb-ink); } .dev .ln { fill: color-mix(in srgb, var(--cc-text) 85%, var(--cc-bg)); } .dev .lh { fill: var(--cc-bg); opacity: 0.8; }
  .dev .pir { fill: var(--hb-ink); opacity: 0.7; } .dev .mt { fill: var(--hb-ink); opacity: 0.6; }
  .dev .dome { fill: color-mix(in srgb, var(--cc-text) 14%, var(--cc-bg)); stroke: var(--hb-ink); stroke-width: 1; }
  .node.off .dev { opacity: 0.45; }
  .nname { fill: var(--cc-text); font-weight: 500; }
  .node.off .nname { fill: var(--cc-dim); }
  .ntxt { fill: var(--cc-sub); font-variant-numeric: tabular-nums; }
  .ntxt.off { fill: var(--cc-dim); font-style: italic; }
  .ntxt.dim { fill: var(--cc-dim); }
  .gi { fill: var(--cc-sub); }
  .gi.al { fill: var(--hb-alarm); }
  .gi.bat { fill: var(--cc-bat-lo); }
  .ndot { fill: var(--cc-accent); stroke: var(--hb-pic); stroke-width: 2; }
  .ndot.pend { fill: color-mix(in srgb, var(--cc-accent) 50%, var(--hb-pic)); }
  .ndot.err { fill: var(--cc-error); }
  .paused .flow, .paused .hub.alarm .ring { animation-play-state: paused; }
  @media (prefers-reduced-motion: reduce) { .flow, .hub.alarm .ring { animation: none; } .sec .chev { transition: none; } }
  .pager { position: absolute; top: 6px; left: 50%; transform: translateX(-50%); display: flex; align-items: center; height: 28px; padding: 0 2px; border-radius: 14px;
    background: color-mix(in srgb, var(--cc-text) 7%, var(--hb-pic)); }
  .pgb { width: 28px; height: 28px; padding: 0; border: 0; border-radius: 14px; background: transparent; color: var(--cc-sub); cursor: pointer; display: grid; place-items: center; --mdc-icon-size: 18px; }
  .pgb ha-icon { display: flex; line-height: 0; }
  .pgb:hover { color: var(--cc-text); background: color-mix(in srgb, var(--cc-text) 6%, transparent); }
  .pgd { width: 14px; height: 28px; padding: 0; border: 0; background: transparent; cursor: pointer; display: grid; place-items: center; }
  .pgd i { width: 6px; height: 6px; border-radius: 50%; background: color-mix(in srgb, var(--cc-text) 30%, transparent); }
  .pgd.on i { width: 8px; height: 8px; background: var(--cc-accent-text); }
  .pgb:focus-visible, .pgd:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -2px; border-radius: 8px; }
  .lens { position: absolute; left: 50%; bottom: 8px; transform: translateX(-50%); display: flex; gap: 2px; padding: 2px; border-radius: 10px;
    background: color-mix(in srgb, var(--cc-text) 7%, var(--hb-pic)); max-width: calc(100% - 20px); box-sizing: border-box; }
  .lo { display: flex; align-items: center; gap: 6px; height: 28px; padding: 0 12px; border: 0; border-radius: 8px; background: transparent; color: var(--cc-sub);
    font-size: var(--fs-xs); cursor: pointer; white-space: nowrap; --mdc-icon-size: 16px; }
  .lo ha-icon { display: flex; line-height: 0; }
  .lo:hover { color: var(--cc-text); background: color-mix(in srgb, var(--cc-text) 6%, transparent); }
  .lo:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .lo.on { background: var(--cc-bg); color: var(--cc-text); font-weight: 600; box-shadow: 0 1px 2px var(--cc-shadow); cursor: default; }
  .lo.ls { padding: 0; width: 28px; justify-content: center; --mdc-icon-size: 18px; }
  .ldot { width: 7px; height: 7px; border-radius: 50%; background: var(--mc); flex: none; }
  .sched { position: absolute; left: 0; top: 0; right: 0; box-sizing: border-box; padding: 10px 14px 6px; display: grid; align-content: center; gap: 2px; }
  .sr { display: grid; grid-template-columns: 40px minmax(0, 1fr); align-items: center; gap: 6px; height: 16px; }
  .sd { font-size: var(--fs-xs); color: var(--cc-sub); line-height: 16px; }
  .sb { position: relative; height: 12px; border-radius: 3px; overflow: hidden; }
  .sg { position: absolute; top: 0; bottom: 0; box-shadow: -1px 0 0 var(--hb-pic), 1px 0 0 var(--hb-pic); }
  .stk { position: relative; height: 16px; }
  .stk span { position: absolute; top: 0; font-size: var(--fs-xs); color: var(--cc-sub); line-height: 16px; white-space: nowrap; }
  .slg { display: flex; flex-wrap: wrap; justify-content: center; gap: 2px 12px; margin-top: 4px; font-size: var(--fs-xs); color: var(--cc-sub); }
  .slg span { display: inline-flex; align-items: center; gap: 5px; }
  .slg i { width: 10px; height: 10px; border-radius: 2px; }
  .toast { position: absolute; left: 10px; right: 10px; z-index: 4; display: flex; align-items: flex-start; gap: 6px; padding: 6px 10px; border-radius: 10px;
    background: var(--cc-bg); box-shadow: 0 2px 8px var(--cc-shadow); color: var(--cc-error-text); font-size: var(--fs-xs); line-height: 1.35; --mdc-icon-size: 16px; }
  .toast ha-icon { display: flex; line-height: 0; flex: none; }
  .confirm { position: absolute; inset: 0; z-index: 5; display: grid; grid-template-rows: auto minmax(0, 1fr) auto; gap: 8px; padding: 12px 14px;
    background: color-mix(in srgb, var(--cc-bg) 96%, transparent); }
  .cfh { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: var(--fs-sm); color: var(--cc-warn-text); --mdc-icon-size: 20px; }
  .cfh ha-icon { display: flex; line-height: 0; }
  .cfl { overflow: auto; display: grid; align-content: start; gap: 6px; }
  .cfi { display: flex; align-items: flex-start; gap: 8px; font-size: var(--fs-sm); line-height: 1.3; --mdc-icon-size: 16px; }
  .cfi ha-icon { display: flex; line-height: 0; flex: none; margin-top: 1px; color: var(--hb-alarm); }
  .cfb { display: flex; justify-content: flex-end; gap: 8px; }
  .medit .cfh { color: var(--cc-text); }
  .medit .cfh ha-icon { color: var(--cc-sub); }
  .medit .mclose { margin-left: auto; }
  .medit .note { padding: 0; }
  .mlist { gap: 2px; }
  .mrow { display: grid; grid-template-columns: minmax(0, 1fr) 32px; grid-template-areas: "in del" "sub sub"; align-items: center; column-gap: 8px;
    padding: 2px 4px; border-radius: 10px; border: 1px solid transparent; }
  .mrow.staged { background: color-mix(in srgb, var(--cc-accent) 6%, transparent); }
  .mrow.failed { border-color: var(--cc-error); }
  .mrow .mname { grid-area: in; height: 34px; box-sizing: border-box; padding: 0 10px; border-radius: 8px; border: 1px solid var(--cc-field-line); background: var(--cc-field);
    color: var(--cc-text); font: inherit; font-size: var(--fs-sm); min-width: 0; }
  .mrow .mname:focus { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .mrow .mname:disabled { color: var(--cc-sub); }
  .mrow > .btn, .mrow > .btn-ph { grid-area: del; }
  .msub { grid-area: sub; min-height: 15px; padding: 0 2px; font-size: var(--fs-xs); line-height: 1.25; color: var(--cc-sub); }
  .msub.err { color: var(--cc-error-text); font-weight: 500; }
  .msub.full { grid-column: 1 / -1; grid-row: 1 / -1; padding: 8px 2px; }
  .madd { grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; height: 34px; padding: 0 8px; border: 1px dashed var(--cc-field-line); border-radius: 8px;
    background: transparent; color: var(--cc-accent-text); font-size: var(--fs-sm); cursor: pointer; --mdc-icon-size: 18px; }
  .madd ha-icon { display: flex; line-height: 0; }
  .madd:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .madd:disabled { color: var(--cc-sub); cursor: default; }

  /* Section toggles: the camera card's segmented strip; one open at a time, they never move */
  .secs { display: flex; gap: 2px; margin: 8px 10px; padding: 2px; border-radius: 10px; background: var(--cc-track); }
  .sec { position: relative; flex: 1 1 0; display: flex; align-items: center; justify-content: center; gap: 6px; min-width: 0; height: 30px;
    padding: 0 6px; border: 0; border-radius: 8px; background: transparent; color: var(--cc-sub); font-size: var(--fs-sm); cursor: pointer; --mdc-icon-size: 18px; }
  .sec ha-icon { display: flex; flex: none; width: 18px; height: 18px; line-height: 0; }
  .sec .chev { width: 16px; height: 16px; --mdc-icon-size: 16px; transition: transform 0.2s; opacity: 0.7; }
  .sec.on .chev { transform: rotate(180deg); }
  .sec span { white-space: nowrap; }
  .secs.fit-name .chev, .secs.fit-icon .chev, .secs.fit-icon .sec span { display: none; }
  .sec:hover { color: var(--cc-text); background: color-mix(in srgb, var(--cc-text) 6%, transparent); }
  .sec:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .sec.on { background: var(--cc-sel); color: var(--cc-text); font-weight: 600; }
  .sec.err { color: var(--cc-error-text); }
  .tdot { position: absolute; top: 4px; right: 5px; width: 6px; height: 6px; border-radius: 50%; background: var(--cc-accent); display: none; }
  .sec.stg .tdot, .stab.stg .tdot { display: block; }
  .sec.err .tdot, .stab.err .tdot { display: block; background: var(--cc-error); }
  .sname { padding: 0 6px 6px; font-size: var(--fs-xs); font-weight: 600; color: var(--cc-sub); }

  .sect { padding: 0 10px 12px; }
  .msg { padding: 16px 6px; color: var(--cc-sub); font-size: var(--fs-sm); }
  .note { display: flex; align-items: center; gap: 6px; padding: 6px 6px 0; font-size: var(--fs-xs); color: var(--cc-sub); --mdc-icon-size: 14px; }
  .note ha-icon { display: flex; line-height: 0; }
  /* Panes of a section share one grid cell: the tallest decides the height, switching never moves the card */
  .stack { display: grid; }
  .stack > .pane { grid-area: 1 / 1; min-width: 0; }
  .stack > .pane[hidden-pane] { visibility: hidden; }
  .stack > .pane.narrowp { justify-self: center; width: 100%; max-width: 560px; }
  .dhead { position: relative; padding: 0 0 4px; }
  .dpick { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 36px; padding: 0 8px; border: 1px solid var(--cc-field-line); border-radius: 10px;
    background: var(--cc-field); color: var(--cc-text); cursor: pointer; --mdc-icon-size: 18px; text-align: left; box-sizing: border-box; }
  .dpick ha-icon { display: flex; line-height: 0; flex: none; color: var(--cc-sub); }
  .dpick .ddc { margin-left: auto; }
  .dpick:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 1px; }
  .dn { font-weight: 600; font-size: var(--fs-sm); white-space: nowrap; }
  .dm { color: var(--cc-sub); font-size: var(--fs-xs); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 0 1 auto; }
  .chip { display: inline-flex; align-items: center; gap: 3px; height: 22px; padding: 0 8px; border-radius: 11px; font-size: var(--fs-xs); white-space: nowrap; --mdc-icon-size: 14px;
    background: var(--cc-track); color: var(--cc-text); }
  .chip ha-icon { display: flex; line-height: 0; color: var(--cc-sub); }
  .chip.off { background: color-mix(in srgb, var(--cc-text) 8%, transparent); color: var(--cc-sub); }
  .chip.lo { background: color-mix(in srgb, var(--cc-bat-lo) 14%, transparent); color: color-mix(in srgb, var(--cc-bat-lo) 60%, var(--cc-text)); }
  .chip.lo ha-icon { color: inherit; }
  .ddw { position: relative; }
  .ddl { position: absolute; left: 0; right: 0; top: calc(100% + 2px); z-index: 7; display: flex; flex-direction: column; gap: 4px; padding: 4px; border-radius: 10px;
    overflow: hidden; background: var(--cc-bg); box-shadow: 0 4px 16px var(--cc-shadow); border: 1px solid var(--cc-line); box-sizing: border-box; }
  .ddl.up { top: auto; bottom: calc(100% + 2px); }
  .ddi { flex: 1 1 auto; min-height: 0; display: grid; grid-template-columns: minmax(0, 1fr); align-content: start; gap: 1px; overflow-x: hidden; overflow-y: auto; }
  .ddf { flex: none; height: 32px; padding: 0 8px; border-radius: 7px; border: 1px solid var(--cc-field-line); background: var(--cc-field); color: var(--cc-text);
    font: inherit; font-size: var(--fs-sm); box-sizing: border-box; min-width: 0; }
  .ddf:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .ddn { padding: 8px; font-size: var(--fs-xs); color: var(--cc-sub); }
  .ddo > span:not(.later) { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ddo:focus-visible > span:not(.later) { white-space: normal; overflow-wrap: anywhere; padding: 4px 0; }
  .ddo[hidden] { display: none; }
  .ddgrow { flex: none; }
  .ddo { display: flex; align-items: center; gap: 8px; min-height: 34px; min-width: 0; padding: 0 8px; border: 0; border-radius: 7px; cursor: pointer;
    background: transparent; color: var(--cc-text); font-size: var(--fs-sm); text-align: left; --mdc-icon-size: 16px; }
  .ddo ha-icon { display: flex; line-height: 0; color: var(--cc-sub); }
  .ddo.off span:first-of-type { color: var(--cc-dim); }
  .ddo .later { margin-left: auto; font-size: var(--fs-xs); color: var(--cc-sub); }
  .ddo:hover, .ddo:focus-visible { background: color-mix(in srgb, var(--cc-text) 8%, transparent); outline: none; }
  .ddo[aria-selected=true] { font-weight: 600; background: var(--cc-sel-quiet); }
  .ddo .chk { margin-left: auto; color: var(--cc-accent-text); }

  /* Matrix: actions × modes; the previewed mode's column tinted */
  .mx { display: grid; align-items: stretch; margin-top: 2px; }
  .mh { display: flex; align-items: center; justify-content: center; gap: 4px; height: 30px; padding: 0 2px; border: 0; border-radius: 8px 8px 0 0; background: transparent;
    color: var(--cc-sub); font-size: var(--fs-xs); cursor: pointer; min-width: 0; }
  .mh span { white-space: nowrap; }
  .mh.c0 { cursor: default; }
  .mh.pv { color: var(--cc-accent-text); font-weight: 600; background: var(--cc-sel-quiet); }
  .mh:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -2px; }
  .ml { display: flex; align-items: center; gap: 7px; min-height: 36px; padding: 0 4px; font-size: var(--fs-sm); line-height: 1.2; border-top: 1px solid var(--cc-line); --mdc-icon-size: 18px; min-width: 0; }
  .ml ha-icon { display: flex; line-height: 0; flex: none; color: var(--cc-sub); }
  .ml ha-icon.al { color: var(--hb-alarm); }
  .ml span { min-width: 0; }
  .ml.none { color: var(--cc-sub); font-size: var(--fs-xs); grid-column: 1 / -1; }
  .cellw { display: grid; place-items: center; border-top: 1px solid var(--cc-line); min-height: 36px; }
  .cellw.pv { background: var(--cc-sel-quiet); }
  .cell { width: 30px; height: 30px; padding: 0; display: grid; place-items: center; border-radius: 8px; cursor: pointer; --mdc-icon-size: 18px;
    border: 1.5px solid var(--cc-sw-off-line); background: transparent; color: var(--cc-sw-on-thumb); }
  .cell ha-icon { display: flex; line-height: 0; }
  .cell.on { background: var(--cc-sw-on); border-color: var(--cc-sw-on-line); }
  .cell.staged { box-shadow: 0 0 0 2px var(--cc-bg), 0 0 0 4px var(--cc-accent); }
  .cell.pending { opacity: 0.55; }
  .cell.failed { box-shadow: 0 0 0 2px var(--cc-bg), 0 0 0 4px var(--cc-error); }
  .cell:disabled { cursor: default; border-style: dashed; border-color: var(--cc-line); }
  .na { color: var(--cc-sub); font-size: var(--fs-xs); }
  .cell:focus-visible, .dcell:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 2px; }
  .dcell { min-width: 40px; height: 28px; padding: 0 4px; border: 1px solid transparent; border-radius: 8px; background: transparent; color: var(--cc-text);
    font-size: var(--fs-sm); font-variant-numeric: tabular-nums; cursor: pointer; white-space: nowrap; }
  .dcell small { font-size: var(--fs-xs); color: var(--cc-sub); margin-left: 1px; }
  .dcell:hover { background: color-mix(in srgb, var(--cc-text) 7%, transparent); }
  .dcell:disabled { color: var(--cc-sub); cursor: default; }
  .dcell.ch { border-color: var(--cc-accent); }
  .dcell.staged { color: var(--cc-accent-text); font-weight: 700; }
  .dcell.failed { color: var(--cc-error-text); border-color: var(--cc-error); }

  /* Rows: the camera card's settings rows */
  .ctl-rows { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); align-items: start; gap: 4px 16px; }
  .row { display: grid; grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "name val" "ctl ctl"; align-items: center; column-gap: 8px;
    padding: 2px 6px 4px; border-radius: 10px; border: 1px solid transparent; --ctl-accent: var(--cc-accent); }
  .row.slot { margin-top: 6px; min-height: 76px; box-sizing: border-box; }
  .row.slot .rname { min-height: 36px; flex-wrap: wrap; column-gap: 7px; row-gap: 0; }
  .row.slot .rctl { gap: 12px; }
  .row.staged { background: color-mix(in srgb, var(--cc-accent) 6%, transparent); }
  .row.failed { border-color: var(--cc-error); }
  .row.muted .rn, .row.muted .rval { color: var(--cc-sub); }
  .rname { grid-area: name; display: flex; align-items: center; gap: 7px; min-width: 0; min-height: 26px; --mdc-icon-size: 18px; }
  .rname ha-icon { display: flex; line-height: 0; flex: none; color: var(--cc-sub); }
  .rn { min-width: 0; font-size: var(--fs-sm); line-height: 1.2; }
  .rsub { flex: none; font-size: var(--fs-xs); line-height: 1.2; color: var(--cc-sub); white-space: nowrap; }
  .rsub.err { color: var(--cc-error-text); font-weight: 500; }
  .rval { grid-area: val; font-size: var(--fs-sm); font-weight: 500; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .rval.unrep { color: var(--cc-sub); font-weight: 400; font-size: var(--fs-xs); }
  .row.staged .rval { color: var(--cc-accent-text); font-weight: 700; }
  .row.na .rn, .row.na .rval { color: var(--cc-sub); }
  .row.na .rctl { opacity: 0.45; }
  .rctl { grid-area: ctl; position: relative; display: flex; align-items: center; height: 32px; min-width: 0; }
  .row.tgrow { grid-template-areas: "name val"; padding-bottom: 2px; min-height: 36px; }
  .tgw { grid-area: val; display: flex; align-items: center; gap: 8px; }
  .ghead { grid-column: 1 / -1; padding: 8px 6px 0; color: var(--cc-sub); font-size: var(--fs-xs); font-weight: 600; }
  .ghead:first-child { padding-top: 0; }
  .tg { flex: none; position: relative; width: 36px; height: 20px; padding: 0; box-sizing: border-box; border: 1px solid var(--cc-sw-off-line);
    border-radius: 10px; cursor: pointer; background: var(--cc-sw-off); transition: background 0.15s; }
  .tg i { position: absolute; left: 2px; top: 2px; width: 14px; height: 14px; border-radius: 50%; background: var(--cc-sw-off-thumb); box-shadow: 0 1px 2px var(--cc-shadow); transition: left 0.15s; }
  .tg.on { background: var(--cc-sw-on); border-color: var(--cc-sw-on-line); }
  .tg.on i { left: 18px; background: var(--cc-sw-on-thumb); }
  .tg.unk i { left: 10px; opacity: 0.6; }
  .row.staged .tg { box-shadow: 0 0 0 2px var(--cc-accent); }
  .row.failed .tg { box-shadow: 0 0 0 2px var(--cc-error); }
  .tg:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) { .tg, .tg i { transition: none; } }
  .seg { display: grid; grid-auto-flow: column; grid-auto-columns: 1fr; gap: 2px; width: 100%; height: 30px; padding: 2px; box-sizing: border-box; border-radius: 9px; background: var(--cc-track); }
  .opt { display: flex; align-items: center; justify-content: center; gap: 5px; min-width: 0; padding: 0 2px; border: 1px solid transparent;
    border-radius: 7px; background: transparent; color: var(--cc-sub); font-size: var(--fs-xs); cursor: pointer; }
  .opt span { white-space: nowrap; }
  .opt:hover { color: var(--cc-text); background: color-mix(in srgb, var(--cc-text) 7%, transparent); }
  .opt:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .opt[aria-pressed="true"] { background: var(--cc-sel); color: var(--cc-text); font-weight: 500; }
  .row.staged .opt[aria-pressed="true"] { border-color: var(--cc-accent); color: var(--cc-accent-text); }
  .rctl .ddw { width: 100%; }
  .dd { display: flex; align-items: center; justify-content: space-between; gap: 6px; width: 100%; height: 32px; padding: 0 8px; border-radius: 8px; border: 1px solid var(--cc-field-line);
    background: var(--cc-field); color: var(--cc-text); font-size: var(--fs-sm); cursor: pointer; --mdc-icon-size: 18px; box-sizing: border-box; text-align: left; }
  .dd span { white-space: nowrap; }
  .dd ha-icon { display: flex; line-height: 0; color: var(--cc-sub); }
  .dd:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 1px; }
  .row.staged .dd { border-color: var(--cc-accent); color: var(--cc-accent-text); font-weight: 600; }
  .ctl-range { -webkit-appearance: none; appearance: none; display: block; flex: 1 1 auto; width: 100%; min-width: 0; height: 32px; margin: 0; background: transparent; cursor: pointer; --pos: 50%; }
  .ctl-range:focus { outline: none; }
  .ctl-range::-webkit-slider-runnable-track { height: 4px; border-radius: 2px; background: linear-gradient(to right, var(--ctl-accent) var(--pos), var(--cc-range) var(--pos)); }
  .ctl-range::-webkit-slider-thumb { -webkit-appearance: none; width: 18px; height: 18px; margin-top: -7px; border: 2px solid var(--cc-bg); border-radius: 50%; background: var(--ctl-accent); box-shadow: 0 1px 3px var(--cc-shadow); }
  .ctl-range::-moz-range-track { height: 4px; border-radius: 2px; background: var(--cc-range); }
  .ctl-range::-moz-range-progress { height: 4px; border-radius: 2px; background: var(--ctl-accent); }
  .ctl-range::-moz-range-thumb { width: 14px; height: 14px; border: 2px solid var(--cc-bg); border-radius: 50%; background: var(--ctl-accent); }
  .ctl-range:focus-visible::-webkit-slider-thumb { box-shadow: 0 0 0 3px color-mix(in srgb, var(--cc-accent) 45%, transparent); }
  .ctl-range.unk::-webkit-slider-thumb { opacity: 0.35; }
  .ctl-range.unk::-webkit-slider-runnable-track { background: var(--cc-range); }

  /* Second-level tabs: the camera card's lighter underlined row */
  .stabs { display: flex; flex-wrap: wrap; gap: 2px 0; margin: 0 0 6px; padding: 2px 0 0; }
  .stab { position: relative; flex: 1 1 auto; display: flex; align-items: center; justify-content: center; gap: 4px; min-width: 0; height: 32px;
    padding: 0 8px; border: 0; border-radius: 6px 6px 0 0; background: transparent; color: var(--cc-sub); font-size: var(--fs-xs); font-weight: 400;
    cursor: pointer; box-shadow: inset 0 -1px 0 var(--cc-line); --mdc-icon-size: 16px; }
  .stab ha-icon { display: flex; flex: none; width: 16px; height: 16px; line-height: 0; }
  .stab span { white-space: nowrap; }
  .stab:hover { color: var(--cc-text); background: color-mix(in srgb, var(--cc-text) 5%, transparent); }
  .stab:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -2px; }
  .stab.on { color: var(--cc-accent-text); font-weight: 600; box-shadow: inset 0 -2px 0 var(--cc-accent-text); flex: 1 0 auto; }
  .stab.err { color: var(--cc-error-text); }
  .stab.on.err { box-shadow: inset 0 -2px 0 var(--cc-error); }
  .stitle { display: none; }
  .stabs.fit1 .stab span { display: none; }
  .stabs.fit1 .stab { flex: 1 1 0; padding: 0; }
  .stabs.fit1 .stitle { display: block; flex: 1 0 100%; box-sizing: border-box; padding: 4px 6px 0; font-size: var(--fs-xs); font-weight: 600; color: var(--cc-sub); }
  .xcam { display: grid; gap: 6px; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--cc-line); }
  .xh { padding: 0 6px; font-size: var(--fs-xs); font-weight: 600; color: var(--cc-sub); }
  .xchip { display: inline-flex; align-items: center; gap: 4px; min-height: 30px; padding: 0 10px 0 8px; border-radius: 15px; border: 1px solid var(--cc-field-line);
    background: transparent; color: var(--cc-text); font-size: var(--fs-xs); cursor: pointer; white-space: nowrap; --mdc-icon-size: 14px; }
  .xchip ha-icon { display: flex; line-height: 0; color: var(--cc-sub); }
  .xchip[aria-pressed="true"] { background: var(--cc-sel); border-color: transparent; font-weight: 500; }
  .xchip[aria-pressed="true"] ha-icon { color: var(--cc-accent-text); }
  .xchip:disabled { cursor: default; }
  .xchip:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: 1px; }
  .xchip.sm { min-height: 28px; padding: 0 9px; }
  .xchip.unk { color: var(--cc-sub); font-style: italic; cursor: default; }
  .xgl { height: 132px; overflow-y: auto; display: grid; align-content: start; gap: 4px; padding: 0 6px; }
  .xgrow { display: grid; grid-template-columns: 16px minmax(0, 1fr) 32px; align-items: start; gap: 6px; padding: 2px 0; border-top: 1px solid var(--cc-line); }
  .xgrow:first-child { border-top: 0; }
  .xgn { font-size: var(--fs-xs); font-weight: 600; color: var(--cc-sub); line-height: 30px; text-align: center; }
  .xgm { display: flex; flex-wrap: wrap; gap: 4px; min-width: 0; }
  .xnone { padding: 6px 0; font-size: var(--fs-xs); color: var(--cc-sub); }
  .gnew.sm { min-height: 34px; font-size: var(--fs-xs); }
  .gnew.sm ha-icon { width: 26px; height: 26px; }
  .gnew:disabled { color: var(--cc-sub); cursor: default; }
  .xfoot { display: flex; flex-wrap: wrap; align-items: center; gap: 2px 12px; min-height: 34px; padding: 0 6px; font-size: var(--fs-xs); color: var(--cc-sub); }
  .xtime { display: inline-flex; align-items: center; gap: 5px; --mdc-icon-size: 14px; white-space: nowrap; }
  .xtime ha-icon { display: flex; line-height: 0; }
  .xst.err { color: var(--cc-error-text); font-weight: 500; }
  .xst.warn { color: var(--cc-warn-text); font-weight: 500; }

  /* Schedule section: the week, the default, one slot form */
  .schw { display: grid; gap: 8px; margin: 0 auto; max-width: 560px; padding: 0 2px; border-radius: 10px; }
  .sdef { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 10px; }
  .slab { display: inline-flex; align-items: center; gap: 7px; font-size: var(--fs-sm); --mdc-icon-size: 18px; white-space: nowrap; }
  .slab ha-icon { display: flex; line-height: 0; color: var(--cc-sub); }
  .swk { display: grid; gap: 3px; }
  .swk .sr { height: 20px; }
  .swk .sb { height: 16px; background-clip: padding-box; }
  .swk .sg { padding: 0; border: 0; cursor: pointer; border-radius: 0; min-width: 3px; }
  .swk .sg.sel { box-shadow: inset 0 0 0 2px var(--cc-text); z-index: 1; }
  .schw .slg { justify-content: flex-start; margin-top: 0; }
  .sfrm { display: grid; gap: 6px; padding-top: 8px; border-top: 1px solid var(--cc-line); }
  .sdays { height: 32px; }
  .stimes { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 6px 8px; }
  .sfl { display: grid; gap: 2px; min-width: 0; }
  .sfl > span { font-size: var(--fs-xs); color: var(--cc-sub); padding-left: 2px; }
  .sfl.smode { grid-column: 1 / -1; }
  .sbar { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; grid-template-areas: "msg msg msg" ". del add"; align-items: center; gap: 4px 6px; }
  .sbar [data-sact=del] { grid-area: del; } .sbar [data-sact=add], .sbar [data-sact=save] { grid-area: add; }
  /* three lines kept for the form's note or the HomeBase's refusal text, so the form never changes height */
  .smsg { grid-area: msg; height: 48px; overflow-y: auto; font-size: var(--fs-xs); line-height: 16px; color: var(--cc-sub); }
  .smsg.warn { color: var(--cc-warn-text); font-weight: 500; }
  .smsg.err { color: var(--cc-error-text); font-weight: 500; }
  .sbar .btn:disabled { opacity: 0.5; cursor: default; }
  .schw.failed .sfrm { border-top-color: var(--cc-error); }
  .dd:disabled { cursor: default; color: var(--cc-sub); }
  .chips { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 6px; padding: 0 6px; }
  .clab { font-size: var(--fs-xs); color: var(--cc-sub); font-weight: 600; margin-right: 2px; }

  /* Groups */
  .glist { display: grid; align-content: start; gap: 4px; }
  .grow { display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto; align-items: center; gap: 10px; min-height: 48px; padding: 2px 6px;
    border-radius: 10px; border: 1px solid transparent; }
  .grow.staged { background: color-mix(in srgb, var(--cc-accent) 6%, transparent); }
  .grow.failed { border-color: var(--cc-error); }
  .gi2 { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; background: var(--cc-track); --mdc-icon-size: 18px; color: var(--cc-sub); }
  .gi2 ha-icon { display: flex; line-height: 0; }
  .gtx { display: grid; min-width: 0; }
  .gn { font-size: var(--fs-sm); font-weight: 500; overflow-wrap: anywhere; }
  .gs { font-size: var(--fs-xs); color: var(--cc-sub); overflow-wrap: anywhere; }
  .gs.err { color: var(--cc-error-text); font-weight: 500; }
  .gact { display: grid; place-items: center; width: 32px; height: 32px; color: var(--cc-accent-text); --mdc-icon-size: 20px; }
  .gact ha-icon { display: flex; line-height: 0; }
  .btn.sm { min-height: 32px; padding: 0 12px 0 10px; font-size: var(--fs-xs); }
  .btn.icon.sm { padding: 0; }
  .gnew { display: flex; align-items: center; gap: 10px; min-height: 44px; padding: 0 6px; border: 0; border-radius: 10px; background: transparent; color: var(--cc-accent-text);
    font-size: var(--fs-sm); cursor: pointer; --mdc-icon-size: 18px; }
  .gnew ha-icon { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; border: 1px dashed var(--cc-field-line); line-height: 0; }
  .gnew:hover { background: color-mix(in srgb, var(--cc-text) 5%, transparent); }
  .gnew:focus-visible { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .gnew.staged { background: color-mix(in srgb, var(--cc-accent) 6%, transparent); }
  .gedit { display: grid; gap: 6px; }
  .ghd { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px; }
  .gname { height: 36px; box-sizing: border-box; padding: 0 10px; border-radius: 8px; border: 1px solid var(--cc-field-line); background: var(--cc-field); color: var(--cc-text); font: inherit; font-size: var(--fs-sm); min-width: 0; }
  .gname:focus { outline: 2px solid var(--cc-accent); outline-offset: -1px; }
  .btn-ph { width: 32px; }
  .glog { display: flex; align-items: center; gap: 6px; padding: 2px 6px; color: var(--cc-sub); font-size: var(--fs-xs); --mdc-icon-size: 14px; }
  .glog ha-icon { display: flex; line-height: 0; }

  /* Status */
  .stl { display: grid; gap: 8px; padding: 2px 4px; }
  .srow { display: grid; grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "name val" "bar bar"; align-items: center; column-gap: 8px; row-gap: 4px; padding: 0 2px; }
  .srow .sv { grid-area: val; font-size: var(--fs-xs); color: var(--cc-sub); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .bar { grid-area: bar; height: 6px; border-radius: 3px; background: var(--cc-range); overflow: hidden; }
  .bar i { display: block; height: 100%; border-radius: 3px; background: var(--cc-accent); }
  .sline { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 16px; min-height: 28px; padding: 0 2px; font-size: var(--fs-xs); color: var(--cc-sub); --mdc-icon-size: 16px; }
  .sline > span, .link { display: inline-flex; align-items: center; gap: 5px; }
  .sline ha-icon { display: flex; line-height: 0; }
  .link { padding: 2px 6px; border: 0; border-radius: 6px; background: transparent; color: var(--cc-accent-text); font-size: var(--fs-xs); font-weight: 600; cursor: pointer; }
  .link:focus-visible { outline: 2px solid var(--cc-accent); }
  .dlist { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 2px 16px; border-top: 1px solid var(--cc-line); padding-top: 6px; }
  .drow2 { display: flex; align-items: center; gap: 8px; min-height: 40px; padding: 0 2px; --mdc-icon-size: 18px; }
  .drow2 .ki { display: flex; line-height: 0; color: var(--cc-sub); }
  .drow2 .dn { display: grid; min-width: 0; flex: 1 1 auto; white-space: normal; font-weight: 400; }
  .dsub { font-size: var(--fs-xs); color: var(--cc-sub); overflow-wrap: anywhere; }
  .drow2.off .rn { color: var(--cc-sub); }
  .bat { display: inline-flex; align-items: center; gap: 3px; font-size: var(--fs-xs); font-variant-numeric: tabular-nums; --mdc-icon-size: 16px; }
  .bat ha-icon { display: flex; line-height: 0; } .bat.hi ha-icon { color: var(--cc-bat-hi); } .bat.mid ha-icon { color: var(--cc-bat-mid); } .bat.lo ha-icon { color: var(--cc-bat-lo); }
  .offw { display: inline-flex; align-items: center; gap: 4px; font-size: var(--fs-xs); color: var(--cc-warn-text); font-weight: 500; --mdc-icon-size: 16px; }
  .offw ha-icon { display: flex; line-height: 0; }
`;

// ================== EDITOR ==================
const EDITOR_SCHEMA = [
  { name: 'device_id', selector: { device: { filter: [{ integration: DOMAIN }], entity: [{ domain: 'alarm_control_panel', integration: DOMAIN }] } } },
  { name: 'name', selector: { text: {} } },
];
const EDITOR_LABELS = { device_id: 'HomeBase', name: 'Name' };
const EDITOR_HELPERS = { name: 'Header name; empty uses the HomeBase\'s name' };

class EufyHomebaseCardEditor extends HTMLElement {
  setConfig(config) { this._config = { ...(config || {}) }; this._sync(); }
  set hass(hass) { this._hass = hass; this._sync(); }
  // HA sets hass before setConfig: nothing reads the config until it exists
  _sync() {
    if (!this._hass) return;
    if (!this._form) {
      const root = this.shadowRoot || this.attachShadow({ mode: 'open' });
      this._form = document.createElement('ha-form');
      this._form.computeLabel = s => EDITOR_LABELS[s.name] || s.name;
      this._form.computeHelper = s => EDITOR_HELPERS[s.name];
      this._form.addEventListener('value-changed', (ev) => {
        ev.stopPropagation();
        const next = { ...this._config };
        Object.entries(ev.detail.value || {}).forEach(([k, v]) => { if (k === 'type') return; if (v === '' || v == null) delete next[k]; else next[k] = v; });
        this._emit(next);
      });
      root.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = EDITOR_SCHEMA;
    if (!this._config) return;
    // One station: pick it when the config names none
    if (!this._config.device_id && !this._config.entity) {
      const s = stations(this._hass);
      if (s.length === 1) { this._emit({ ...this._config, device_id: s[0] }); return; }
    }
    this._form.data = { ...this._config };
  }
  _emit(next) {
    this._config = next;
    if (this._form) this._form.data = { ...next };
    this.dispatchEvent(new CustomEvent('config-changed', { detail: { config: next }, bubbles: true, composed: true }));
  }
}

if (!customElements.get('eufy-homebase-card-editor')) customElements.define('eufy-homebase-card-editor', EufyHomebaseCardEditor);
if (!customElements.get('eufy-homebase-card')) customElements.define('eufy-homebase-card', EufyHomebaseCard);
window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === 'eufy-homebase-card')) {
  window.customCards.push({ type: 'eufy-homebase-card', name: 'eufy HomeBase',
    description: 'Security mode, what each paired device does per mode, arming groups, the HomeBase settings and status (staged)', preview: false });
}
console.info(`%c eufy-homebase-card %c ${CARD_VERSION} `, 'background:#273a60;color:#fff', 'background:#ddd;color:#000');
