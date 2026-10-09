/**
 * Schedule card for the OptoV integration.
 *
 * Edits the weekly programmes the integration publishes as schedule sensors. Nothing about a
 * controller lives in this file: which programmes exist, what they and their levels are
 * called, how many windows a day holds and on what time grid all come from the sensor's
 * attributes (and through them from the controller catalog). The card's own UI strings come from
 * the integration's translation files, day names from the browser locale.
 *
 *   type: custom:optov-schedule-card            every programme, one tab each
 *   type: custom:optov-schedule-card
 *   entities:                                        any subset, one tab each
 *     - sensor.<schedule sensor>
 *     - entity: sensor.<schedule sensor>             with a tab label of your own
 *       name: Hot water
 *   names:                                           tab labels while showing them all
 *     sensor.<schedule sensor>: Hot water
 *   entity: sensor.<schedule sensor>                 one programme (older form)
 *   actual_entity: sensor.<any>                      optional reading shown in the header
 *   demand_entity: sensor.<any>                      optional setpoint shown in the header
 *   title: <text>                                    optional, defaults to the controller name
 */

const CARD_TYPE = 'optov-schedule-card';
const EDITOR_TYPE = 'optov-schedule-card-editor';
const FAULT_CARD_TYPE = 'optov-fault-history-card';
const FAULT_EDITOR_TYPE = 'optov-fault-history-card-editor';
const DOMAIN = 'optov';
const DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

/* ------------------------------------------------------------------------------------------
 * Updates. A dashboard that stays open keeps the copy of these cards it loaded, even after the
 * integration was updated and serves a new one: a custom element cannot be defined twice in a
 * page. The integration puts a stamp in the cards' URL and publishes the same stamp on the
 * sensors the cards read, so a copy that is behind can say so and offer a reload -- which is
 * all it takes, since the new stamp makes the browser fetch the new file.
 *
 * The reload happens by itself as soon as an outdated card is on screen, once per new version:
 * the stamp it reloaded for is kept for the browser session, so a reload that did not bring the
 * new file (a proxy, a stubborn cache) cannot turn into a loop. Not while something is being
 * edited, and not where the session store is unavailable; the notice with its button is what
 * remains then.
 * ---------------------------------------------------------------------------------------- */
const RELOADED_FOR = 'optov-cards-reloaded-for';

const LOADED_VERSION = (() => {
  try {
    return new URL(import.meta.url).searchParams.get('v');
  } catch (err) {
    return null;
  }
})();

// This copy's build, for telling which of two copies in one page is newer: the stamp, which is
// the card file's own timestamp. A copy loaded without one counts as the oldest.
const CARD_BUILD = Number(LOADED_VERSION) || 0;

/*
 * A page can end up with more than one copy of these cards: an open dashboard after an update, a
 * copy a browser or app still holds in its cache and runs before the current one arrives. The
 * first copy to run defines the elements, and a defined element cannot be defined again, so the
 * current code would be ignored. Instead, a newer copy hands its methods to the classes already
 * defined; every card on the page runs the newer code from its next update on, without a reload.
 */
function adopt(name, cls) {
  const existing = customElements.get(name);
  cls.BUILD = CARD_BUILD;
  if (!existing) {
    customElements.define(name, cls);
    return;
  }
  if (existing === cls || !(CARD_BUILD > (Number(existing.BUILD) || 0))) return;
  for (const key of Object.getOwnPropertyNames(cls.prototype)) {
    if (key !== 'constructor') {
      Object.defineProperty(existing.prototype, key, Object.getOwnPropertyDescriptor(cls.prototype, key));
    }
  }
  for (const key of Object.getOwnPropertyNames(cls)) {
    if (!['length', 'name', 'prototype'].includes(key)) {
      Object.defineProperty(existing, key, Object.getOwnPropertyDescriptor(cls, key));
    }
  }
}

/**
 * Fills in what this copy's methods expect on an instance an older copy constructed. Returns
 * true the first time, when the instance has just come over and its config should be applied
 * again with this copy's setConfig.
 */
function adoptState(instance, defaults) {
  if (instance._build === CARD_BUILD) return false;
  instance._build = CARD_BUILD;
  for (const [key, value] of Object.entries(defaults())) {
    if (instance[key] === undefined) instance[key] = value;
  }
  return true;
}

const SCHEDULE_DEFAULTS = () => ({
  _config: {},
  _activeDay: todayKey(),
  _scope: null,
  _selectedEntityId: null,
  _edits: {},
  _saving: false,
  _status: null,
  _conflict: null,
  _reading: false,
  _signature: null,
  _strings: null,
  _holidayEdit: null,
  _holidayOpen: null,
  _holidayBusy: false,
});
const FAULT_DEFAULTS = () => ({ _config: {}, _signature: null, _strings: null });
const EDITOR_DEFAULTS = () => ({ _config: {}, _formReady: false, _strings: null });

/** Whether Home Assistant serves a newer copy of these cards than the one running here. */
function isOutdated(stateObj) {
  const served = Number(stateObj && stateObj.attributes && stateObj.attributes.card_version);
  return Boolean(served && CARD_BUILD && served > CARD_BUILD);
}

/** Reload the page for a newer card, unless `busy` or already tried for this version. */
function reloadIfOutdated(stateObj, busy) {
  if (busy || !isOutdated(stateObj)) return false;
  const served = String(stateObj.attributes.card_version);
  try {
    if (window.sessionStorage.getItem(RELOADED_FOR) === served) return false;
    window.sessionStorage.setItem(RELOADED_FOR, served);
  } catch (err) {
    return false;
  }
  window.location.reload();
  return true;
}

function updateNotice(t) {
  return `<div class="update">
      <span>${escapeHtml(t('card_updated'))}</span>
      <button class="button primary reload">${escapeHtml(t('reload'))}</button>
    </div>`;
}

function bindReload(root) {
  root.querySelectorAll('.reload').forEach((el) => el.addEventListener('click', () => window.location.reload()));
}

const UPDATE_STYLE = `
  .update { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 12px;
            margin-bottom: 12px; border-radius: 8px; border: 1px solid var(--primary-color); font-size: 0.9rem; }
  .update .button { padding: 6px 14px; border-radius: 8px; border: 1px solid var(--primary-color); font: inherit;
            font-weight: 500; cursor: pointer; background: var(--primary-color); color: var(--text-primary-color, #fff); }
`;
const MINUTES_PER_DAY = 24 * 60;

/* ------------------------------------------------------------------------------------------
 * Translations: the integration's translations/<lang>.json, category "card", fetched through
 * the same websocket call the frontend uses for its own backend translations. English is
 * merged underneath as the fallback for keys a language does not have.
 * ---------------------------------------------------------------------------------------- */
const translationCache = new Map();

function languageOf(hass) {
  const raw = (hass && ((hass.locale && hass.locale.language) || hass.language)) || 'en';
  return String(raw).toLowerCase();
}

async function fetchCardStrings(hass, language) {
  const resources = await hass.callWS({
    type: 'frontend/get_translations',
    language,
    category: 'card',
    integration: DOMAIN,
  });
  const prefix = `component.${DOMAIN}.card.`;
  const out = {};
  for (const [key, value] of Object.entries(resources.resources || {})) {
    if (key.startsWith(prefix)) out[key.slice(prefix.length)] = value;
  }
  return out;
}

function loadStrings(hass) {
  const language = languageOf(hass);
  if (!translationCache.has(language)) {
    translationCache.set(
      language,
      (async () => {
        const [own, english] = await Promise.all([
          fetchCardStrings(hass, language).catch(() => ({})),
          language.startsWith('en') ? Promise.resolve({}) : fetchCardStrings(hass, 'en').catch(() => ({})),
        ]);
        return { ...english, ...own };
      })()
    );
  }
  return translationCache.get(language);
}

function makeTranslator(strings) {
  return (key, vars = {}) => {
    let text = strings[key] !== undefined ? strings[key] : key;
    for (const [name, value] of Object.entries(vars)) text = text.replace(`{${name}}`, String(value));
    return text;
  };
}

/* ------------------------------------------------------------------------------------------ */

/** Localised weekday names keyed by day token. 2024-01-01 was a Monday. */
function weekdayNames(lang, style) {
  const fmt = new Intl.DateTimeFormat(lang, { weekday: style });
  const names = {};
  DAY_KEYS.forEach((key, i) => {
    names[key] = fmt.format(new Date(Date.UTC(2024, 0, 1 + i, 12)));
  });
  return names;
}

function todayKey() {
  return DAY_KEYS[(new Date().getDay() + 6) % 7];
}

/** Today as an ISO date in local time, comparable with a date entity's state. */
function isoToday() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** "24 Dec – 6 Jan 2027": the year only where it is not this one. Empty when neither is set. */
function holidayRange(start, end, lang) {
  const thisYear = new Date().getFullYear();
  const format = (iso) => {
    const [y, m, d] = iso.split('-').map(Number);
    const options = { day: 'numeric', month: 'short', ...(y !== thisYear ? { year: 'numeric' } : {}) };
    return new Date(y, m - 1, d).toLocaleDateString(lang, options);
  };
  return [start, end].filter(Boolean).map(format).join(' – ');
}

function isScheduleEntity(stateObj) {
  return Boolean(
    stateObj && stateObj.attributes && stateObj.attributes.weekly_schedule !== undefined && stateObj.attributes.schedule
  );
}

function hasBeenRead(stateObj) {
  return stateObj.state !== 'unavailable' && stateObj.state !== 'unknown';
}

/** Every programme the integration has published, in a stable order. */
function discoverSchedules(hass) {
  return Object.values(hass.states)
    .filter(isScheduleEntity)
    .sort((a, b) => {
      const da = String(a.attributes.device_name || '');
      const db = String(b.attributes.device_name || '');
      if (da !== db) return da.localeCompare(db);
      return String(a.attributes.base_address || '').localeCompare(String(b.attributes.base_address || ''));
    });
}

/** `entities` in any of its accepted shapes -> a list of {entity, name?} in the given order. */
function normaliseEntities(configured) {
  if (!configured) return [];
  const list = Array.isArray(configured) ? configured : [configured];
  return list
    .map((item) => (typeof item === 'string' ? { entity: item } : item))
    .filter((item) => item && item.entity);
}

/**
 * Short labels for a set of programme names: the words they all begin with are dropped
 * ("Schaltzeiten HK1", "Schaltzeiten WW" -> "HK1", "WW"). A single name is kept whole.
 */
function shortLabels(names) {
  if (names.length < 2) return names.slice();
  const split = names.map((n) => String(n).trim().split(/\s+/));
  let common = 0;
  while (split.every((w) => w.length > common + 1 && w[common] === split[0][common])) common += 1;
  return split.map((w) => w.slice(common).join(' ') || w.join(' '));
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function toMinutes(time) {
  if (!time) return 0;
  const [h, m] = String(time).split(':').map((p) => parseInt(p, 10) || 0);
  return h * 60 + m;
}

function fromMinutes(total) {
  const h = Math.floor(total / 60);
  const m = total % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

/**
 * How a level is drawn. The catalog carries a colour per level, but a fixed palette looks
 * pasted-on inside a Home Assistant theme, so the card uses two cues of its own: the series
 * palette Home Assistant paints its history and statistics charts with, and the bar's height.
 *
 * `--color-1` upward is that palette; it is part of the frontend's base styles, identical in
 * light and dark, so a level looks like a series on any other chart in the dashboard. The
 * literals are only a fallback for a theme old enough not to define them.
 *
 * Height is the second cue and the one that survives a screenshot in greyscale: the higher the
 * level, the taller the bar, exactly as the controller's own display draws its programme. It is
 * also what makes overlapping windows readable -- a short bar in front of a tall one still
 * leaves the tall one visible above it -- which a single-height bar cannot show at all.
 */
const LEVEL_COLORS = [
  'var(--color-1, #4269d0)',
  'var(--color-2, #f4bd4a)',
  'var(--color-3, #ff725c)',
  'var(--color-4, #6cc5b0)',
];

/** Rank each selectable level, lowest first, so colour and height follow the level order. */
function levelRanks(levels) {
  const sorted = levels.slice().sort((a, b) => a.value - b.value);
  return Object.fromEntries(sorted.map((m, index) => [m.value, index]));
}

function levelColor(ranks, level) {
  const rank = ranks[level];
  if (rank === undefined) return 'var(--disabled-color, #9e9e9e)';
  return LEVEL_COLORS[Math.min(rank, LEVEL_COLORS.length - 1)];
}

/** Percentage of the bar's height, spread over however many levels the programme has. */
function levelHeight(ranks, level, count) {
  const rank = ranks[level];
  if (rank === undefined) return 30;
  return 35 + (65 * (rank + 1)) / Math.max(1, count);
}

/**
 * Which days a save is written to. The controller has no notion of grouped days -- it stores
 * seven independent ones, and any "Mon-Fri" grouping is a display convention over days whose
 * contents happen to match. So a group here is a save-time scope, not something read back:
 * pick the days, and the same windows are written to each of them.
 *
 * The split relies on the day order the sensor publishes, which is the controller's own and
 * starts at Monday, so the first five are the working week and the last two the weekend.
 */
const SCOPES = ['day', 'weekdays', 'weekend', 'week'];

function scopeDays(scope, dayKeys, activeDay) {
  if (scope === 'weekdays') return dayKeys.slice(0, 5);
  if (scope === 'weekend') return dayKeys.slice(5);
  if (scope === 'week') return dayKeys.slice();
  return [activeDay];
}

function scopeLabel(scope, dayKeys, activeDay, shortNames, longNames, t) {
  if (scope === 'weekdays') return `${shortNames[dayKeys[0]]}\u2013${shortNames[dayKeys[4]]}`;
  if (scope === 'weekend') return `${shortNames[dayKeys[5]]}\u2013${shortNames[dayKeys[6]]}`;
  if (scope === 'week') return t('scope_week');
  return longNames[activeDay] || activeDay;
}

/**
 * The scope the week is already in, so the card can preselect it. A group is only offered when
 * every day in it genuinely holds the same programme, which means saving in that scope rewrites
 * what is already there rather than quietly changing days the user was not looking at.
 */
function detectScope(dayKeys, activeDay, stored) {
  const same = (days) => {
    const first = fingerprint(stored[days[0]]);
    return days.every((d) => fingerprint(stored[d]) === first);
  };
  if (same(dayKeys)) return 'week';
  const weekdays = dayKeys.slice(0, 5);
  const weekend = dayKeys.slice(5);
  if (weekdays.includes(activeDay) && same(weekdays)) return 'weekdays';
  if (weekend.includes(activeDay) && same(weekend)) return 'weekend';
  return 'day';
}

/** Two days are the same programme when their windows are; used to show what already matches. */
function fingerprint(windows) {
  return (windows || [])
    .map((w) => `${w.start}-${w.end}:${w.mode}`)
    .sort()
    .join('|');
}

function timeOptions(step) {
  const out = [];
  for (let m = 0; m < MINUTES_PER_DAY; m += step) out.push(fromMinutes(m));
  out.push('24:00');
  return out;
}

/** The fault-history sensors: a decoded buffer plus the address it was read from. */
function discoverFaultHistories(hass) {
  return Object.values(hass.states)
    .filter((s) => s && s.attributes && Array.isArray(s.attributes.entries) && s.attributes.buffer_address)
    .sort((a, b) => String(a.entity_id).localeCompare(String(b.entity_id)));
}

const FAULT_STYLE = `
  :host { display: block; }
  ha-card { padding: 16px; box-sizing: border-box; }
  .header { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 12px; }
  .header > ha-icon { color: var(--primary-color); flex: none; margin-top: 2px; }
  .heading { min-width: 0; }
  .title { font-size: 1.15rem; font-weight: 500; line-height: 1.3; }
  .subtitle { font-size: 0.82rem; color: var(--secondary-text-color); margin-top: 2px; }
  .faults { display: flex; flex-direction: column; overflow-y: auto; }
  .fault { display: grid; grid-template-columns: auto 1fr auto; align-items: baseline; gap: 10px;
           padding: 9px 2px; border-top: 1px solid var(--divider-color); }
  .fault:first-child { border-top: none; }
  .when { font-size: 0.82rem; color: var(--secondary-text-color); white-space: nowrap;
          font-variant-numeric: tabular-nums; }
  .what { min-width: 0; overflow-wrap: anywhere; }
  .code { font-size: 0.75rem; font-weight: 600; padding: 2px 7px; border-radius: 10px;
          background: var(--secondary-background-color); color: var(--secondary-text-color);
          white-space: nowrap; font-variant-numeric: tabular-nums; }
  .empty { padding: 12px; text-align: center; font-size: 0.9rem; color: var(--secondary-text-color);
           border: 1px dashed var(--divider-color); border-radius: 8px; }
`;

const STYLE = `
  :host { display: block; }
  ha-card { padding: 16px; box-sizing: border-box; }
  .header { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 12px; }
  .header > ha-icon { color: var(--primary-color); flex: none; margin-top: 2px; }
  .heading { min-width: 0; }
  .title { font-size: 1.15rem; font-weight: 500; line-height: 1.3; }
  .subtitle { font-size: 0.82rem; color: var(--secondary-text-color); margin-top: 2px; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; border-radius: 16px;
          border: 1px solid var(--divider-color); font-size: 0.85rem; cursor: pointer; }
  .chip:hover { border-color: var(--primary-color); }
  .chip ha-icon { --mdc-icon-size: 16px; color: var(--primary-color); }
  .chip.demand ha-icon { color: var(--warning-color, var(--accent-color)); }
  .chip .label { color: var(--secondary-text-color); }
  .chip .value { font-weight: 500; }
  .tabs { display: flex; gap: 6px; margin-bottom: 12px; }
  .tabs.programmes { flex-wrap: wrap; }
  .tabs.days { display: grid; grid-template-columns: repeat(7, 1fr); }
  .tab { padding: 8px 12px; border-radius: 8px; border: 1px solid var(--divider-color); background: none;
         color: var(--secondary-text-color); font: inherit; font-size: 0.9rem; font-weight: 500; cursor: pointer;
         text-align: center; white-space: nowrap; }
  .tabs.programmes .tab { flex: 1 1 auto; }
  .tab:hover { color: var(--primary-text-color); border-color: var(--primary-color); }
  .tab.active { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .tab.dirty::after { content: ' •'; }
  /* Days that already hold switching windows, so an empty day is visible at a glance. The
     plain background is the fallback for a browser without color-mix. */
  .tab.programmed { color: var(--primary-text-color); border-color: var(--color-1, #4269d0); }
  .tab.programmed { background: color-mix(in srgb, var(--color-1, #4269d0) 14%, transparent); }
  .tab.programmed.active { background: var(--primary-color); border-color: var(--primary-color); }
  /* The other days a save goes to, so the choice under "Apply to" shows up on the days themselves. */
  .tab.inscope { border-color: var(--primary-color); box-shadow: inset 0 -3px 0 var(--primary-color); }
  .timeline { margin: 12px 0; }
  .bar { position: relative; height: 38px; border-radius: 6px; overflow: hidden; background: var(--secondary-background-color); }
  .segment { position: absolute; bottom: 0; display: flex; align-items: center; justify-content: center;
             font-size: 0.7rem; font-weight: 500; color: var(--text-primary-color, #fff); overflow: hidden; white-space: nowrap;
             background: var(--primary-color); border-radius: 3px 3px 0 0; }
  .ticks { display: flex; justify-content: space-between; font-size: 0.72rem; color: var(--secondary-text-color); margin-top: 4px; }
  .matching { font-size: 0.8rem; color: var(--secondary-text-color); margin: -6px 0 10px; }
  /* The label above and the choices as one segmented row, which stays on one line on a phone. */
  .scope { display: flex; flex-direction: column; gap: 6px; }
  .scope .label { font-size: 0.85rem; font-weight: 500; color: var(--secondary-text-color); }
  .scope .choices { display: flex; border: 1px solid var(--divider-color); border-radius: 10px; overflow: hidden; }
  .scope button { flex: 1 1 auto; min-width: 0; padding: 8px 6px; border: none; border-left: 1px solid var(--divider-color);
                  background: var(--card-background-color); color: var(--secondary-text-color);
                  font: inherit; font-size: 0.85rem; cursor: pointer; white-space: nowrap;
                  overflow: hidden; text-overflow: ellipsis; }
  .scope button:first-child { border-left: none; }
  .scope button:hover { color: var(--primary-text-color); }
  .scope button.active { background: var(--primary-color); color: var(--text-primary-color, #fff); font-weight: 500; }
  .windows { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
  .window { display: grid; grid-template-columns: 24px 1fr auto 1fr 1.2fr 36px; align-items: center; gap: 8px;
            padding: 6px 10px; border-radius: 8px; border: 1px solid var(--divider-color);
            border-left: 4px solid var(--divider-color); }
  .window.no-level { grid-template-columns: 24px 1fr auto 1fr 36px; }
  .index { font-size: 0.85rem; color: var(--secondary-text-color); text-align: center; }
  .sep { font-size: 0.85rem; color: var(--secondary-text-color); }
  select { width: 100%; box-sizing: border-box; padding: 7px 8px; border-radius: 6px; font: inherit; font-size: 0.95rem;
           background: var(--card-background-color); color: var(--primary-text-color); border: 1px solid var(--divider-color); }
  select:focus { outline: none; border-color: var(--primary-color); }
  .icon-button { width: 36px; height: 36px; display: flex; align-items: center; justify-content: center; border: none;
                 border-radius: 50%; background: none; color: var(--error-color); cursor: pointer; }
  .icon-button:hover { background: var(--secondary-background-color); }
  .empty { padding: 12px; text-align: center; font-size: 0.9rem; color: var(--secondary-text-color);
           border: 1px dashed var(--divider-color); border-radius: 8px; }
  .add { width: 100%; padding: 10px; margin-bottom: 12px; border-radius: 8px; border: 1px dashed var(--divider-color);
         background: none; color: var(--primary-text-color); font: inherit; font-size: 0.9rem; cursor: pointer; }
  .add:hover { border-color: var(--primary-color); }
  /* Which days a change goes to, right above the buttons that send it. */
  .savebar { padding: 12px; margin-bottom: 12px; border-radius: 12px; background: var(--secondary-background-color); }
  .footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .status { font-size: 0.9rem; color: var(--secondary-text-color); min-width: 0; }
  .status.ok { color: var(--success-color, var(--primary-color)); }
  .status.error { color: var(--error-color); }
  .actions { display: flex; gap: 8px; flex: none; }
  .button { padding: 9px 18px; border-radius: 8px; border: 1px solid var(--divider-color); background: none;
            color: var(--primary-text-color); font: inherit; font-weight: 500; cursor: pointer; }
  .button.primary { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .button:disabled { opacity: 0.5; cursor: default; }
  .conflict { display: flex; flex-direction: column; gap: 10px; padding: 10px 12px; margin-bottom: 12px;
              border-radius: 8px; border: 1px solid var(--warning-color, #ffa600);
              font-size: 0.9rem; color: var(--primary-text-color); }
  .conflict .actions { justify-content: flex-end; flex-wrap: wrap; }
  .read-at { display: flex; align-items: center; justify-content: flex-end; gap: 4px; padding-top: 6px;
             font-size: 0.8rem; color: var(--secondary-text-color); }
  .reread { width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; border: none;
            border-radius: 50%; background: none; color: var(--secondary-text-color); cursor: pointer;
            --mdc-icon-size: 18px; }
  .reread:hover { background: var(--secondary-background-color); }
  .reread:disabled { opacity: 0.5; cursor: default; }
  /* The holiday sits above the week because it overrides the whole programme for its days. */
  .holiday { margin-bottom: 12px; border: 1px solid var(--divider-color); border-radius: 10px; }
  .holiday.planned { border-color: var(--primary-color); }
  .holiday.planned { background: color-mix(in srgb, var(--primary-color) 8%, transparent); }
  .holiday-toggle { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 40px; padding: 6px 12px;
                    box-sizing: border-box; border: none; background: none; color: var(--primary-text-color);
                    font: inherit; text-align: left; cursor: pointer; }
  .holiday-toggle ha-icon { flex: none; --mdc-icon-size: 20px; color: var(--secondary-text-color); }
  .holiday.planned .holiday-toggle .icon { color: var(--primary-color); }
  .holiday-toggle .label { font-size: 0.9rem; font-weight: 500; }
  .holiday-toggle .value { margin-left: auto; font-size: 0.9rem; color: var(--secondary-text-color);
                           font-variant-numeric: tabular-nums; white-space: nowrap; }
  .holiday.planned .holiday-toggle .value { color: var(--primary-text-color); }
  .holiday.dirty .holiday-toggle .value::after { content: ' •'; }
  .holiday-body { display: flex; flex-direction: column; gap: 8px; padding: 0 12px 12px; }
  .holiday-days { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 8px; }
  .holiday-clear { align-self: flex-end; display: flex; align-items: center; gap: 6px; padding: 6px 14px;
                   border-radius: 8px; border: 1px solid var(--error-color); background: none; color: var(--error-color);
                   font: inherit; font-size: 0.85rem; font-weight: 500; cursor: pointer; --mdc-icon-size: 18px; }
  .holiday-clear:hover { background: color-mix(in srgb, var(--error-color) 10%, transparent); }
  .holiday-clear:disabled { opacity: 0.5; cursor: default; }
  .holiday input[type="date"] { width: 100%; min-width: 0; box-sizing: border-box; padding: 7px 8px; border-radius: 6px;
             font: inherit; font-size: 0.95rem; background: var(--card-background-color); color: var(--primary-text-color);
             border: 1px solid var(--divider-color); }
  .holiday input[type="date"]:focus { outline: none; border-color: var(--primary-color); }
`;

class OptoVScheduleCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._activeDay = todayKey();
    this._scope = null; // null = follow the pattern the week is already in
    this._selectedEntityId = null;
    this._edits = {}; // "entity_id|day" -> windows being edited
    this._saving = false;
    this._status = null; // { kind: 'ok' | 'error', text }
    // A save the controller refused because the programme was changed there since it was
    // read: what the user wanted to write, kept for "overwrite". { entityId, windows, targets,
    // days }. The card meanwhile shows the controller's version.
    this._conflict = null;
    this._reading = false;
    this._signature = null;
    this._strings = null;
    // Holiday dates being typed, before they are saved: { entityId, start, end } with ISO dates
    // or '' for none. entityId is the programme on screen, whose circuit the holiday is.
    this._holidayEdit = null;
    // The programme whose holiday dates are unfolded; folded, the holiday is one line.
    this._holidayOpen = null;
    this._holidayBusy = false;
    this._build = CARD_BUILD;
  }

  static getConfigElement() {
    return document.createElement(EDITOR_TYPE);
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { ...(config || {}) };
    // `entities` picks any subset, in the given order; `entity` is the single-programme form
    // and is kept working. Neither means every programme the integration has read. An entry
    // may be a plain entity id or `{entity, name}`, where the name replaces the tab label --
    // the same shape Home Assistant's own entities card uses.
    const chosen = normaliseEntities(this._config.entities || this._config.entity);
    this._chosen = chosen.map((item) => item.entity);
    // Tab labels come from either form: a `name` on the entry, or the `names` map, which is
    // what naming looks like when no programme is picked out and the card shows them all.
    this._labels = {
      ...(this._config.names || {}),
      ...Object.fromEntries(chosen.filter((i) => i.name).map((i) => [i.entity, i.name])),
    };
    this._signature = null;
    if (this._hass) this._render();
  }

  set hass(hass) {
    if (adoptState(this, SCHEDULE_DEFAULTS)) this.setConfig(this._config);
    this._hass = hass;
    if (!this._strings) {
      loadStrings(hass).then((strings) => {
        this._strings = strings;
        this._signature = null;
        this._render();
      });
      return;
    }
    // Home Assistant pushes a new hass object on every state change anywhere. Re-rendering
    // each time would reset open dropdowns mid-edit, so only redraw when something this card
    // shows has actually changed.
    const signature = this._computeSignature();
    if (signature !== this._signature) {
      this._signature = signature;
      this._render();
    }
  }

  getCardSize() {
    return 6;
  }

  _computeSignature() {
    const parts = [];
    for (const ent of this._entities()) {
      parts.push(ent.entity_id, ent.last_updated);
      const holiday = ent.attributes.holiday || {};
      for (const id of [holiday.start, holiday.end]) {
        const s = id && this._hass.states[id];
        if (s) parts.push(id, s.state);
      }
    }
    for (const id of [this._config.actual_entity, this._config.demand_entity]) {
      const s = id && this._hass.states[id];
      if (s) parts.push(id, s.state, s.attributes.unit_of_measurement);
    }
    return parts.join('|');
  }

  _entities() {
    if (!this._hass) return [];
    if (this._chosen && this._chosen.length) {
      return this._chosen.map((id) => this._hass.states[id]).filter(isScheduleEntity);
    }
    return discoverSchedules(this._hass).filter(hasBeenRead);
  }

  _activeEntity() {
    const entities = this._entities();
    if (!entities.length) return null;
    if (this._selectedEntityId) {
      const hit = entities.find((e) => e.entity_id === this._selectedEntityId);
      if (hit) return hit;
    }
    return entities[0];
  }

  _editKey(entity) {
    return `${entity.entity_id}|${this._activeDay}`;
  }

  _windowsOf(entity) {
    const key = this._editKey(entity);
    if (this._edits[key]) return this._edits[key];
    const sched = entity.attributes.weekly_schedule || {};
    const stored = sched[this._activeDay];
    return stored ? stored.map((w) => ({ ...w })) : [];
  }

  _setWindows(entity, windows) {
    this._edits[this._editKey(entity)] = windows;
    this._status = null;
    this._render();
  }

  _isDirty(entity, day) {
    return Boolean(this._edits[`${entity.entity_id}|${day}`]);
  }

  /** Levels a window may be set to: everything except the level meaning "nothing". */
  _activeLevels(entity) {
    const modes = Array.isArray(entity.attributes.modes) ? entity.attributes.modes : [];
    const off = entity.attributes.default_level;
    return modes.filter((m) => m.value !== off);
  }

  async _save(entity, overwrite = null) {
    const t = makeTranslator(this._strings || {});
    const lang = languageOf(this._hass);
    const windows = (overwrite ? overwrite.windows : this._windowsOf(entity))
      .slice()
      .sort((a, b) => toMinutes(a.start) - toMinutes(b.start));
    for (let i = 0; i < windows.length; i += 1) {
      if (toMinutes(windows[i].end) <= toMinutes(windows[i].start)) {
        this._status = { kind: 'error', text: t('invalid_window', { n: i + 1 }) };
        this._render();
        return;
      }
    }
    const attrs = entity.attributes;
    const dayKeys = Array.isArray(attrs.days) && attrs.days.length === 7 ? attrs.days : DAY_KEYS;
    const scope = overwrite
      ? overwrite.scope
      : this._scope || detectScope(dayKeys, this._activeDay, attrs.weekly_schedule || {});
    const targets = overwrite ? overwrite.targets : scopeDays(scope, dayKeys, this._activeDay);
    const shortNames = weekdayNames(lang, 'short');
    const longNames = weekdayNames(lang, 'long');

    this._saving = true;
    this._conflict = null;
    // The integration reads the programme from the controller before writing, to see whether
    // it was changed there; that is most of the wait.
    this._status = { kind: '', text: t('checking') };
    this._render();
    try {
      await this._hass.callService(DOMAIN, 'set_schedule_day', {
        schedule: attrs.schedule,
        day: targets,
        windows: windows.map((w, i) => ({ window: i + 1, start: w.start, end: w.end, mode: w.mode })),
        force: Boolean(overwrite),
      });
      // Every target day now holds what was just written, so no pending edit of one can
      // still be meaningful.
      for (const day of targets) delete this._edits[`${entity.entity_id}|${day}`];
      this._scope = null; // null = follow the pattern the week is already in
      this._confirm(t('saved', { day: scopeLabel(scope, dayKeys, this._activeDay, shortNames, longNames, t) }));
    } catch (err) {
      if (err && err.translation_key === 'schedule_changed') {
        // Nothing was written, and the entity already carries the controller's version. Show
        // that instead of the edit, and keep the edit for "overwrite".
        for (const day of targets) delete this._edits[`${entity.entity_id}|${day}`];
        const placeholders = err.translation_placeholders || {};
        this._conflict = { entityId: entity.entity_id, windows, targets, scope, days: placeholders.days || '' };
        this._status = null;
      } else {
        this._status = { kind: 'error', text: t('save_failed', { err: (err && err.message) || err }) };
      }
    } finally {
      this._saving = false;
      this._render();
    }
  }

  /**
   * A confirmation of what this card just wrote. It answers the person who pressed the button,
   * so it goes again by itself rather than staying on while the values change from elsewhere.
   */
  _confirm(text) {
    const status = { kind: 'ok', text };
    this._status = status;
    setTimeout(() => {
      if (this._status === status) {
        this._status = null;
        this._render();
      }
    }, 4000);
  }

  /** The programme's circuit's holiday as its two date entities, if the integration paired them. */
  _holidayOf(entity) {
    const ids = entity.attributes.holiday;
    if (!ids || !ids.start || !ids.end) return null;
    const start = this._hass.states[ids.start];
    const end = this._hass.states[ids.end];
    if (!start || !end || start.state === 'unavailable' || end.state === 'unavailable') return null;
    const value = (s) => (/^\d{4}-\d{2}-\d{2}$/.test(s.state) ? s.state : '');
    return { start, end, startValue: value(start), endValue: value(end) };
  }

  /** The holiday as shown: the dates being edited, else the controller's, and whether they differ. */
  _holidayState(entity, holiday) {
    if (!holiday) return null;
    const edit = this._holidayEdit && this._holidayEdit.entityId === entity.entity_id ? this._holidayEdit : null;
    const start = edit ? edit.start : holiday.startValue;
    const end = edit ? edit.end : holiday.endValue;
    return { start, end, dirty: start !== holiday.startValue || end !== holiday.endValue };
  }

  _holidayEditDirty(entity) {
    const shown = this._holidayState(entity, this._holidayOf(entity));
    return Boolean(shown && shown.dirty);
  }

  /**
   * The card's one save: the holiday dates if they were changed, then the day's programme if it
   * was changed or is to be copied to other days. Only what changed is written.
   */
  async _saveAll(entity) {
    const t = makeTranslator(this._strings || {});
    const holiday = this._holidayOf(entity);
    const wanted = this._holidayState(entity, holiday);
    const holidayDirty = Boolean(wanted && wanted.dirty);
    const attrs = entity.attributes;
    const dayKeys = Array.isArray(attrs.days) && attrs.days.length === 7 ? attrs.days : DAY_KEYS;
    const scope = this._scope || detectScope(dayKeys, this._activeDay, attrs.weekly_schedule || {});
    // A detected scope only means "keep the week's pattern"; on its own it is worth a write, but
    // not when the save is for the holiday and the programme was left as it is.
    const programme =
      this._isDirty(entity, this._activeDay) || (this._scope && this._scope !== 'day') || (!holidayDirty && scope !== 'day');

    if (holidayDirty) {
      const clear = !wanted.start && !wanted.end;
      if (!clear && (!wanted.start || !wanted.end || wanted.end < wanted.start)) {
        this._status = { kind: 'error', text: t('holiday_invalid') };
        this._render();
        return;
      }
      // 1970-01-01 is what the controller holds for "not set", and what clears it.
      const writes = [
        [holiday.start.entity_id, wanted.start || '1970-01-01', holiday.startValue],
        [holiday.end.entity_id, wanted.end || '1970-01-01', holiday.endValue],
      ].filter(([, date, held]) => date !== (held || '1970-01-01'));
      this._holidayBusy = true;
      this._status = null;
      this._render();
      try {
        for (const [entityId, date] of writes) {
          await this._hass.callService('date', 'set_value', { entity_id: entityId, date });
        }
        this._holidayEdit = null;
        this._holidayOpen = null;
        this._confirm(t(clear ? 'holiday_cleared' : 'holiday_saved'));
      } catch (err) {
        this._status = { kind: 'error', text: t('save_failed', { err: (err && err.message) || String(err) }) };
        return;
      } finally {
        this._holidayBusy = false;
        this._render();
      }
    }
    if (programme) await this._save(entity);
  }

  async _reread(entity) {
    const t = makeTranslator(this._strings || {});
    this._reading = true;
    this._status = { kind: '', text: t('loading') };
    this._render();
    try {
      await this._hass.callService(DOMAIN, 'read_schedule', { schedule: entity.attributes.schedule });
      this._status = null;
    } catch (err) {
      this._status = { kind: 'error', text: (err && err.message) || String(err) };
    } finally {
      this._reading = false;
      this._render();
    }
  }

  _render() {
    if (!this._hass || !this._strings) return;
    const t = makeTranslator(this._strings);
    const lang = languageOf(this._hass);
    const entity = this._activeEntity();

    if (!entity) {
      const configured = (this._chosen || []).find((id) => !this._hass.states[id]);
      const pending = !(this._chosen || []).length && discoverSchedules(this._hass).length > 0;
      this.shadowRoot.innerHTML = `
        <style>${STYLE}</style>
        <ha-card>
          <div class="header">
            <ha-icon icon="mdi:calendar-clock"></ha-icon>
            <div class="heading"><div class="title">${escapeHtml(this._config.title || t('title'))}</div></div>
          </div>
          <div class="empty" style="margin-top: 12px;">
            ${escapeHtml(pending ? t('loading') : t('no_entity'))}
            ${configured ? `<div style="margin-top: 6px;"><code>${escapeHtml(configured)}</code></div>` : ''}
            ${!pending && !configured ? `<div style="margin-top: 6px;">${escapeHtml(t('no_entity_hint'))}</div>` : ''}
          </div>
        </ha-card>`;
      return;
    }

    const editing =
      Object.keys(this._edits).length > 0 ||
      Boolean(this._holidayEdit) ||
      Boolean(this._conflict) ||
      this._saving ||
      this._holidayBusy;
    if (reloadIfOutdated(entity, editing)) return;

    const attrs = entity.attributes;
    const entities = this._entities();
    const showProgrammeTabs = entities.length > 1;
    const labels = shortLabels(entities.map((e) => e.attributes.name || e.entity_id));
    const dayKeys = Array.isArray(attrs.days) && attrs.days.length === 7 ? attrs.days : DAY_KEYS;
    const shortNames = weekdayNames(lang, 'short');
    const longNames = weekdayNames(lang, 'long');
    const levels = this._activeLevels(entity);
    const levelLabel = Object.fromEntries((attrs.modes || []).map((m) => [m.value, m.label]));
    const ranks = levelRanks(levels);
    const showLevel = levels.length > 1;
    const maxWindows = Number(attrs.windows_per_day) || 8;
    const step = Number(attrs.minutes_per_step) || 10;
    const options = timeOptions(step);
    const windows = this._windowsOf(entity);
    const dirty = this._isDirty(entity, this._activeDay);

    // Days holding the same programme as the one on screen -- the "Mon-Sun" or "Mon-Fri" of
    // a grouped view; naming them outright says more and needs no rules.
    const stored = attrs.weekly_schedule || {};
    const activeFingerprint = fingerprint(stored[this._activeDay]);
    const matching = dayKeys.filter((d) => d !== this._activeDay && fingerprint(stored[d]) === activeFingerprint);
    const programmed = new Set(dayKeys.filter((d) => (stored[d] || []).length));
    // An explicit choice wins; otherwise follow whatever pattern the week is already in, so
    // editing one of five identical weekdays offers to keep them identical.
    const scope = this._scope || detectScope(dayKeys, this._activeDay, stored);
    // A scope other than the single day is itself a change: copying today's untouched
    // programme onto the working week is the whole point of the control.
    const canSave = dirty || scope !== 'day' || this._holidayEditDirty(entity);
    const saveDays = scopeDays(scope, dayKeys, this._activeDay);
    const conflict = this._conflict && this._conflict.entityId === entity.entity_id ? this._conflict : null;
    const readAt = attrs.read_at
      ? new Date(attrs.read_at).toLocaleString(lang, { dateStyle: 'short', timeStyle: 'short' })
      : '';

    const holiday = this._holidayOf(entity);
    const shownHoliday = this._holidayState(entity, holiday);
    const holidayDirty = Boolean(shownHoliday && shownHoliday.dirty);
    const holidayOpen = Boolean(holiday) && this._holidayOpen === entity.entity_id;
    // Planned: not over yet. A holiday that has passed stays shown, but plainly.
    const holidayPlanned = Boolean(shownHoliday && shownHoliday.end && shownHoliday.end >= isoToday());
    const anyDirty = dirty || holidayDirty;

    const title = this._config.title || attrs.device_name || t('title');
    const programmeName = (this._labels || {})[entity.entity_id] || attrs.name || '';
    const subtitle = `${programmeName} · ${t('days_programmed', { n: entity.state })}`;

    const chip = (entityId, cls, labelKey, defaultIcon) => {
      const s = entityId && this._hass.states[entityId];
      if (!s) return '';
      const unit = s.attributes.unit_of_measurement ? ` ${s.attributes.unit_of_measurement}` : '';
      return `
        <div class="chip ${cls}" data-more-info="${escapeHtml(entityId)}">
          <ha-icon icon="${escapeHtml(s.attributes.icon || defaultIcon)}"></ha-icon>
          <span class="label">${escapeHtml(t(labelKey))}</span>
          <span class="value">${escapeHtml(s.state)}${escapeHtml(unit)}</span>
        </div>`;
    };

    const timeSelect = (cls, index, value) =>
      `<select class="${cls}" data-index="${index}">${options
        .map((o) => `<option value="${o}" ${o === value ? 'selected' : ''}>${o}</option>`)
        .join('')}</select>`;

    this.shadowRoot.innerHTML = `
      <style>${STYLE}${UPDATE_STYLE}</style>
      <ha-card>
        <div class="header">
          <ha-icon icon="mdi:calendar-clock"></ha-icon>
          <div class="heading">
            <div class="title">${escapeHtml(title)}</div>
            <div class="subtitle">${escapeHtml(subtitle)}</div>
          </div>
        </div>
        ${isOutdated(entity) ? updateNotice(t) : ''}

        ${this._config.actual_entity || this._config.demand_entity
        ? `<div class="chips">
                ${chip(this._config.actual_entity, 'actual', 'actual', 'mdi:thermometer')}
                ${chip(this._config.demand_entity, 'demand', 'target', 'mdi:target')}
              </div>`
        : ''
      }

        ${showProgrammeTabs
        ? `<div class="tabs programmes">${entities
          .map((e, i) => {
            const active = e.entity_id === entity.entity_id ? 'active' : '';
            const anyDirty = dayKeys.some((d) => this._isDirty(e, d)) ? 'dirty' : '';
            const label = (this._labels || {})[e.entity_id] || labels[i];
            return `<button class="tab ${active} ${anyDirty}" data-entity="${escapeHtml(e.entity_id)}" title="${escapeHtml(e.attributes.name || '')}">${escapeHtml(label)}</button>`;
          })
          .join('')}</div>`
        : ''
      }

        ${holiday
        ? `<div class="holiday ${holidayPlanned ? 'planned' : ''} ${holidayDirty ? 'dirty' : ''}">
            <button class="holiday-toggle" aria-expanded="${holidayOpen}" ${this._holidayBusy ? 'disabled' : ''}>
              <ha-icon class="icon" icon="mdi:airplane"></ha-icon>
              <span class="label">${escapeHtml(t('holiday'))}</span>
              <span class="value">${escapeHtml(holidayRange(shownHoliday.start, shownHoliday.end, lang) || t('holiday_none'))}</span>
              <ha-icon icon="${holidayOpen ? 'mdi:chevron-up' : 'mdi:pencil'}"></ha-icon>
            </button>
            ${holidayOpen
          ? `<div class="holiday-body">
              <div class="holiday-days">
                <input type="date" class="holiday-start" value="${escapeHtml(shownHoliday.start)}"
                       title="${escapeHtml(holiday.start.attributes.friendly_name || '')}" ${this._holidayBusy ? 'disabled' : ''}>
                <span class="sep">${escapeHtml(t('to'))}</span>
                <input type="date" class="holiday-end" value="${escapeHtml(shownHoliday.end)}"
                       title="${escapeHtml(holiday.end.attributes.friendly_name || '')}" ${this._holidayBusy ? 'disabled' : ''}>
              </div>
              ${shownHoliday.start || shownHoliday.end
            ? `<button class="holiday-clear" ${this._holidayBusy ? 'disabled' : ''}><ha-icon icon="mdi:delete-outline"></ha-icon>${escapeHtml(t('holiday_clear'))}</button>`
            : ''
          }
            </div>`
          : ''
        }
          </div>`
        : ''
      }

        <div class="tabs days">${dayKeys
        .map((d) => {
          const active = d === this._activeDay ? 'active' : '';
          const isDirty = this._isDirty(entity, d) ? 'dirty' : '';
          const has = programmed.has(d) ? 'programmed' : '';
          const inScope = !active && saveDays.includes(d) ? 'inscope' : '';
          return `<button class="tab ${active} ${isDirty} ${has} ${inScope}" data-day="${d}" title="${escapeHtml(longNames[d] || d)}">${escapeHtml(shortNames[d] || d)}</button>`;
        })
        .join('')}</div>

        ${matching.length ? `<div class="matching">${escapeHtml(t('same_as', { days: matching.map((d) => longNames[d] || d).join(', ') }))}</div>` : ''}

        <div class="timeline">
          <div class="bar">${windows
        .slice()
        // Tallest first, so a shorter window overlapping a taller one is drawn in front
        // of it rather than hidden behind it.
        .sort((a, b) => levelHeight(ranks, b.mode, levels.length) - levelHeight(ranks, a.mode, levels.length))
        .map((w) => {
          const start = toMinutes(w.start);
          const end = toMinutes(w.end);
          const left = (start / MINUTES_PER_DAY) * 100;
          const width = Math.max(0, ((end - start) / MINUTES_PER_DAY) * 100);
          const height = levelHeight(ranks, w.mode, levels.length);
          const label = showLevel ? levelLabel[w.mode] || '' : '';
          const style = `left:${left}%;width:${width}%;height:${height}%;background:${levelColor(ranks, w.mode)}`;
          return `<div class="segment" style="${escapeHtml(style)}" title="${escapeHtml(`${w.start} – ${w.end}${label ? ` (${label})` : ''}`)}">${width > 12 && height > 55 ? escapeHtml(label) : ''}</div>`;
        })
        .join('')}</div>
          <div class="ticks"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
        </div>

        <div class="windows">
          ${windows.length
        ? windows
          .map(
            (w, i) => `
              <div class="window ${showLevel ? '' : 'no-level'}" style="border-left-color: ${escapeHtml(levelColor(ranks, w.mode))}">
                <span class="index">${i + 1}</span>
                ${timeSelect('start', i, w.start)}
                <span class="sep">${escapeHtml(t('to'))}</span>
                ${timeSelect('end', i, w.end)}
                ${showLevel
                ? `<select class="level" data-index="${i}">${levels
                  .map((m) => `<option value="${m.value}" ${m.value === w.mode ? 'selected' : ''}>${escapeHtml(m.label)}</option>`)
                  .join('')}</select>`
                : ''
              }
                <button class="icon-button remove" data-index="${i}" title="${escapeHtml(t('remove'))}"><ha-icon icon="mdi:close"></ha-icon></button>
              </div>`
          )
          .join('')
        : `<div class="empty">${escapeHtml(t('no_windows', { day: longNames[this._activeDay] }))}</div>`
      }
        </div>

        ${windows.length < maxWindows
        ? `<button class="add">+ ${escapeHtml(t('add_window', { n: windows.length, max: maxWindows }))}</button>`
        : ''
      }

        ${conflict
        ? `<div class="conflict">
            <div>${escapeHtml(t('changed_on_controller', {
          days: String(conflict.days).split(/,\s*/).map((d) => shortNames[d] || d).join(', '),
        }))}</div>
            <div class="actions">
              <button class="button keep">${escapeHtml(t('keep_controller'))}</button>
              <button class="button primary overwrite">${escapeHtml(t('overwrite'))}</button>
            </div>
          </div>`
        : ''
      }

        <div class="savebar">
          <div class="scope">
            <span class="label">${escapeHtml(t('apply_to'))}</span>
            <div class="choices">${SCOPES.map((value) => {
          const active = value === scope ? 'active' : '';
          const label = scopeLabel(value, dayKeys, this._activeDay, shortNames, longNames, t);
          return `<button class="${active}" data-scope="${value}">${escapeHtml(label)}</button>`;
        }).join('')}</div>
          </div>
        </div>

        <div class="footer">
          <div class="status ${this._status ? this._status.kind : ''}">
            ${escapeHtml(this._status ? this._status.text : anyDirty ? t('unsaved') : '')}
          </div>
          <div class="actions">
            ${anyDirty && !this._saving && !this._holidayBusy ? `<button class="button discard">${escapeHtml(t('discard'))}</button>` : ''}
            <button class="button primary save" ${this._saving || this._reading || this._holidayBusy || !canSave ? 'disabled' : ''}>
              ${escapeHtml(this._saving ? t('saving') : t('save'))}
            </button>
          </div>
        </div>
        ${readAt
        ? `<div class="read-at">
            <span>${escapeHtml(t('read_at', { time: readAt }))}</span>
            <button class="reread" title="${escapeHtml(t('reread'))}" ${this._saving || this._reading ? 'disabled' : ''}>
              <ha-icon icon="mdi:refresh"></ha-icon>
            </button>
          </div>`
        : ''
      }
      </ha-card>`;

    this._bind(entity, levels, step);
  }

  _bind(entity, levels, step) {
    const root = this.shadowRoot;
    bindReload(root);
    const on = (selector, event, handler) =>
      root.querySelectorAll(selector).forEach((el) => el.addEventListener(event, handler));

    on('[data-more-info]', 'click', (e) => {
      const entityId = e.currentTarget.getAttribute('data-more-info');
      this.dispatchEvent(new CustomEvent('hass-more-info', { bubbles: true, composed: true, detail: { entityId } }));
    });

    on('.tabs.programmes .tab', 'click', (e) => {
      this._selectedEntityId = e.currentTarget.getAttribute('data-entity');
      this._scope = null;
      this._status = null;
      this._render();
    });

    on('.scope button', 'click', (e) => {
      this._scope = e.currentTarget.getAttribute('data-scope');
      this._status = null;
      this._render();
    });

    on('.tabs.days .tab', 'click', (e) => {
      this._activeDay = e.currentTarget.getAttribute('data-day');
      // The pattern to follow depends on the day, so an untouched scope re-detects.
      this._scope = null;
      this._status = null;
      this._render();
    });

    const update = (field, numeric) => (e) => {
      const index = parseInt(e.currentTarget.getAttribute('data-index'), 10);
      const windows = this._windowsOf(entity).map((w) => ({ ...w }));
      windows[index][field] = numeric ? Number(e.currentTarget.value) : e.currentTarget.value;
      this._setWindows(entity, windows);
    };
    on('select.start', 'change', update('start', false));
    on('select.end', 'change', update('end', false));
    on('select.level', 'change', update('mode', true));

    on('.remove', 'click', (e) => {
      const index = parseInt(e.currentTarget.getAttribute('data-index'), 10);
      const windows = this._windowsOf(entity).map((w) => ({ ...w }));
      windows.splice(index, 1);
      this._setWindows(entity, windows);
    });

    on('.add', 'click', () => {
      const windows = this._windowsOf(entity).map((w) => ({ ...w }));
      // Continue where the last window ends, two hours long, capped at midnight.
      let start = '06:00';
      let end = '22:00';
      if (windows.length) {
        const lastEnd = toMinutes(windows[windows.length - 1].end);
        if (lastEnd < MINUTES_PER_DAY) {
          start = fromMinutes(lastEnd);
          end = fromMinutes(Math.min(MINUTES_PER_DAY, lastEnd + 120));
        }
      }
      // The usual "on" level: 2 (normal) where the programme has it, else the first one.
      const preferred = levels.find((m) => m.value === 2) || levels[0];
      windows.push({ window: windows.length + 1, start, end, mode: preferred ? preferred.value : 1 });
      this._setWindows(entity, windows);
    });

    on('.discard', 'click', () => {
      delete this._edits[this._editKey(entity)];
      if (this._holidayEdit && this._holidayEdit.entityId === entity.entity_id) this._holidayEdit = null;
      this._status = null;
      this._render();
    });

    on('.save', 'click', () => this._saveAll(entity));
    on('.overwrite', 'click', () => this._save(entity, this._conflict));
    on('.keep', 'click', () => {
      this._conflict = null;
      this._render();
    });
    on('.reread', 'click', () => this._reread(entity));

    const holiday = this._holidayOf(entity);
    const editHoliday = (field) => (e) => {
      const current =
        this._holidayEdit && this._holidayEdit.entityId === entity.entity_id
          ? this._holidayEdit
          : { entityId: entity.entity_id, start: holiday.startValue, end: holiday.endValue };
      this._holidayEdit = { ...current, [field]: e.currentTarget.value };
      this._status = null;
      this._render();
    };
    on('.holiday-start', 'change', editHoliday('start'));
    on('.holiday-end', 'change', editHoliday('end'));
    on('.holiday-toggle', 'click', () => {
      this._holidayOpen = this._holidayOpen === entity.entity_id ? null : entity.entity_id;
      this._render();
    });
    on('.holiday-clear', 'click', () => {
      this._holidayEdit = { entityId: entity.entity_id, start: '', end: '' };
      this._status = null;
      this._render();
    });
  }
}

/**
 * Fault-history card: the controller's own fault buffer as a list.
 *
 * Home Assistant shows an entity's *state* over time, which for this sensor is the newest entry
 * and therefore changes at every restart -- that history is not the fault log. The log is the
 * `entries` attribute, and nothing renders an attribute by itself, so this does.
 *
 *   type: custom:optov-fault-history-card
 *   entity: sensor.<fault history>     optional, the only one is found by itself
 *   hide_codes: [FF]                   optional; what a code means is per controller
 *   max: 30                            optional, how many entries to list
 *   height: 420                        optional, list height in pixels before it scrolls
 *   title: <text>                      optional, defaults to the controller name
 */
class OptoVFaultHistoryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._signature = null;
    this._strings = null;
    this._build = CARD_BUILD;
  }

  static getConfigElement() {
    return document.createElement(FAULT_EDITOR_TYPE);
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = { ...(config || {}) };
    this._hidden = new Set((this._config.hide_codes || []).map((c) => String(c).toUpperCase()));
    this._signature = null;
    if (this._hass) this._render();
  }

  set hass(hass) {
    if (adoptState(this, FAULT_DEFAULTS)) this.setConfig(this._config);
    this._hass = hass;
    if (!this._strings) {
      loadStrings(hass).then((strings) => {
        this._strings = strings;
        this._signature = null;
        this._render();
      });
      return;
    }
    const entity = this._entity();
    const signature = entity ? `${entity.entity_id}|${entity.last_updated}` : '';
    if (signature !== this._signature) {
      this._signature = signature;
      this._render();
    }
  }

  getCardSize() {
    return 6;
  }

  _entity() {
    if (!this._hass) return null;
    if (this._config.entity) return this._hass.states[this._config.entity] || null;
    return discoverFaultHistories(this._hass)[0] || null;
  }

  _render() {
    if (!this._hass || !this._strings) return;
    const t = makeTranslator(this._strings);
    const entity = this._entity();

    if (!entity) {
      this.shadowRoot.innerHTML = `
        <style>${FAULT_STYLE}</style>
        <ha-card>
          <div class="header">
            <ha-icon icon="mdi:history"></ha-icon>
            <div class="heading"><div class="title">${escapeHtml(this._config.title || t('faults_title'))}</div></div>
          </div>
          <div class="empty">${escapeHtml(t('faults_no_entity'))}</div>
        </ha-card>`;
      return;
    }

    if (reloadIfOutdated(entity, false)) return;

    const attrs = entity.attributes;
    const all = Array.isArray(attrs.entries) ? attrs.entries : [];
    const shown = all.filter((e) => !this._hidden.has(String(e.code).toUpperCase()));
    const limited = this._config.max ? shown.slice(0, Number(this._config.max)) : shown;
    const hidden = all.length - shown.length;

    const subtitle = [
      t('faults_count', { n: all.length }),
      hidden ? t('faults_hidden', { n: hidden }) : '',
    ]
      .filter(Boolean)
      .join(' · ');

    this.shadowRoot.innerHTML = `
      <style>${FAULT_STYLE}${UPDATE_STYLE}</style>
      <ha-card>
        <div class="header">
          <ha-icon icon="mdi:history"></ha-icon>
          <div class="heading">
            <div class="title">${escapeHtml(this._config.title || attrs.device_name || t('faults_title'))}</div>
            <div class="subtitle">${escapeHtml(subtitle)}</div>
          </div>
        </div>
        ${isOutdated(entity) ? updateNotice(t) : ''}
        ${limited.length
        ? `<div class="faults" style="max-height: ${Number(this._config.height) || 420}px">${limited
          .map(
            (e) => `
              <div class="fault">
                <span class="when">${escapeHtml(e.timestamp_str || '')}</span>
                <span class="what">${escapeHtml(e.description || '')}</span>
                <span class="code">${escapeHtml(e.code || '')}</span>
              </div>`
          )
          .join('')}</div>`
        : `<div class="empty">${escapeHtml(t('faults_none'))}</div>`
      }
      </ha-card>`;
    bindReload(this.shadowRoot);
  }
}

/** Visual editor for the fault card: the codes to hide come from the buffer itself. */
class OptoVFaultHistoryCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._formReady = false;
    this._strings = null;
    this._build = CARD_BUILD;
  }

  setConfig(config) {
    adoptState(this, EDITOR_DEFAULTS);
    this._config = { ...(config || {}) };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._strings) {
      loadStrings(hass).then((strings) => {
        this._strings = strings;
        this._render();
      });
    }
    this._render();
  }

  async _ensureForm() {
    if (this._formReady || customElements.get('ha-form')) {
      this._formReady = true;
      return;
    }
    try {
      const helpers = await window.loadCardHelpers();
      const card = await helpers.createCardElement({ type: 'entities', entities: [] });
      if (card && card.constructor && card.constructor.getConfigElement) {
        await card.constructor.getConfigElement();
      }
    } catch (err) {
      // ha-form may still arrive once the editor dialog has finished loading.
    }
    this._formReady = true;
    this._render();
  }

  _schema(t) {
    const histories = discoverFaultHistories(this._hass);
    const chosen = this._config.entity
      ? this._hass.states[this._config.entity]
      : histories[0];
    // Offer exactly the codes this controller has logged, each with its own text and how often
    // it occurs, most frequent first. Nothing here knows what a code means or whether it
    // matters -- the catalog does not say -- but the count makes the routine one obvious.
    const seen = new Map();
    for (const entry of (chosen && chosen.attributes.entries) || []) {
      const found = seen.get(entry.code) || { text: entry.description || entry.code, count: 0 };
      found.count += 1;
      seen.set(entry.code, found);
    }
    const codes = [...seen].sort((a, b) => b[1].count - a[1].count);

    const schema = [];
    // Only when there is a choice to make. One controller means one buffer, and a dropdown
    // with a single option does nothing but take up room -- including for a card whose config
    // still names it from an earlier edit.
    if (histories.length > 1) {
      schema.push({
        name: 'entity',
        selector: {
          select: {
            mode: 'dropdown',
            options: histories.map((e) => ({
              value: e.entity_id,
              label: e.attributes.device_name || e.entity_id,
            })),
          },
        },
      });
    }
    schema.push(
      {
        name: 'hide_codes',
        selector: {
          select: {
            multiple: true,
            mode: 'list',
            options: codes.map(([code, { text, count }]) => ({
              value: code,
              label: `${code} · ${text} (${count})`,
            })),
          },
        },
      },
      { name: 'max', selector: { number: { min: 1, max: 100, mode: 'box' } } },
      { name: 'height', selector: { number: { min: 100, max: 2000, step: 20, mode: 'box' } } },
      { name: 'title', selector: { text: {} } }
    );
    return schema;
  }

  _render() {
    if (!this._hass || !this._strings) return;
    if (!this._formReady) {
      this._ensureForm();
      return;
    }
    const t = makeTranslator(this._strings);
    let form = this.shadowRoot.querySelector('ha-form');
    if (!form) {
      this.shadowRoot.innerHTML = '';
      form = document.createElement('ha-form');
      form.addEventListener('value-changed', (e) => this._onChange(e));
      this.shadowRoot.appendChild(form);
    }
    form.hass = this._hass;
    form.schema = this._schema(t);
    form.data = {
      entity: this._config.entity || '',
      hide_codes: this._config.hide_codes || [],
      max: this._config.max,
      height: this._config.height,
      title: this._config.title || '',
    };
    form.computeLabel = (schema) => t(`editor_${schema.name}`);
  }

  _onChange(e) {
    e.stopPropagation();
    const values = e.detail.value || {};
    const config = { type: `custom:${FAULT_CARD_TYPE}` };
    // Only name the buffer when there is more than one to name; otherwise the card finds it
    // and the config stays free of an entity id that can go stale.
    if (values.entity && discoverFaultHistories(this._hass).length > 1) config.entity = values.entity;
    if (Array.isArray(values.hide_codes) && values.hide_codes.length) config.hide_codes = values.hide_codes;
    if (values.max) config.max = Number(values.max);
    if (values.height) config.height = Number(values.height);
    if (values.title) config.title = values.title;
    this._config = config;
    this.dispatchEvent(new CustomEvent('config-changed', { bubbles: true, composed: true, detail: { config } }));
  }
}

/**
 * Visual editor. Offers the programmes the integration has published, so the user picks one
 * by name instead of typing an entity id.
 */
class OptoVScheduleCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._formReady = false;
    this._strings = null;
    this._build = CARD_BUILD;
  }

  setConfig(config) {
    adoptState(this, EDITOR_DEFAULTS);
    this._config = { ...(config || {}) };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._strings) {
      loadStrings(hass).then((strings) => {
        this._strings = strings;
        this._render();
      });
    }
    this._render();
  }

  async _ensureForm() {
    if (this._formReady || customElements.get('ha-form')) {
      this._formReady = true;
      return;
    }
    // ha-form ships with the dashboard editor bundle, which is loaded lazily. Asking the
    // core entities card for its editor pulls that bundle in.
    try {
      const helpers = await window.loadCardHelpers();
      const card = await helpers.createCardElement({ type: 'entities', entities: [] });
      if (card && card.constructor && card.constructor.getConfigElement) {
        await card.constructor.getConfigElement();
      }
    } catch (err) {
      // Fall through: ha-form may still turn up once the editor dialog has loaded.
    }
    this._formReady = true;
    this._render();
  }

  _chosen() {
    return normaliseEntities(this._config.entities || this._config.entity);
  }

  /**
   * The programmes to offer a tab label for: the chosen ones, or -- when none are chosen and
   * the card therefore shows them all -- every programme there is. Naming has to work in that
   * mode too, which is the point of the `names` map.
   */
  _labelTargets() {
    const chosen = this._chosen();
    if (chosen.length) return chosen;
    const names = this._config.names || {};
    return discoverSchedules(this._hass).map((e) => ({ entity: e.entity_id, name: names[e.entity_id] }));
  }

  _schema(t) {
    // Any subset, in the order picked. Nothing selected means every programme the integration
    // has read, which is also what the card does with no configuration at all.
    const options = discoverSchedules(this._hass).map((e) => ({
      value: e.entity_id,
      label: `${e.attributes.device_name ? `${e.attributes.device_name} · ` : ''}${e.attributes.name || e.entity_id}${hasBeenRead(e) ? '' : ' …'}`,
    }));
    const schema = [
      { name: 'entities', selector: { select: { multiple: true, mode: 'list', options } } },
    ];
    // One optional tab label per chosen programme. The field is keyed by position but carries
    // the entity it belongs to, so a name still lands on the right programme when the
    // selection changes in the same edit.
    this._tabFields = this._labelTargets().map((item, index) => ({
      key: `tab_${index}`,
      entity: item.entity,
      name: item.name || '',
    }));
    for (const field of this._tabFields) {
      const state = this._hass.states[field.entity];
      schema.push({
        name: field.key,
        programme: (state && state.attributes.name) || field.entity,
        selector: { text: {} },
      });
    }
    schema.push(
      { name: 'actual_entity', selector: { entity: { domain: ['sensor', 'number'] } } },
      { name: 'demand_entity', selector: { entity: { domain: ['sensor', 'number'] } } },
      { name: 'title', selector: { text: {} } }
    );
    return schema;
  }

  _render() {
    if (!this._hass || !this._strings) return;
    if (!this._formReady) {
      this._ensureForm();
      return;
    }
    const t = makeTranslator(this._strings);
    let form = this.shadowRoot.querySelector('ha-form');
    if (!form) {
      this.shadowRoot.innerHTML = '';
      form = document.createElement('ha-form');
      form.addEventListener('value-changed', (e) => this._onChange(e));
      this.shadowRoot.appendChild(form);
    }
    form.hass = this._hass;
    form.schema = this._schema(t);
    form.data = {
      entities: this._chosen().map((item) => item.entity),
      ...Object.fromEntries(this._tabFields.map((f) => [f.key, f.name])),
      actual_entity: this._config.actual_entity || '',
      demand_entity: this._config.demand_entity || '',
      title: this._config.title || '',
    };
    form.computeLabel = (schema) =>
      schema.programme ? `${t('editor_tab_name')}: ${schema.programme}` : t(`editor_${schema.name}`);
  }

  _onChange(e) {
    e.stopPropagation();
    const values = e.detail.value || {};
    const config = { type: `custom:${CARD_TYPE}` };
    // Names are carried by entity, not by position, so reordering or removing a programme in
    // the same edit cannot move a label onto a different one.
    const names = {};
    for (const field of this._tabFields || []) {
      const name = (values[field.key] || '').trim();
      if (name) names[field.entity] = name;
    }
    if (Array.isArray(values.entities) && values.entities.length) {
      // A chosen programme carries its label with it, the way Home Assistant's entities card
      // writes one.
      config.entities = values.entities.map((id) => (names[id] ? { entity: id, name: names[id] } : id));
    } else if (Object.keys(names).length) {
      // Showing them all: the labels cannot hang off a list that is deliberately absent, so
      // they go in a map of their own and the card keeps discovering programmes by itself.
      config.names = names;
    }
    for (const key of ['actual_entity', 'demand_entity', 'title']) {
      if (values[key]) config[key] = values[key];
    }
    this._config = config;
    this.dispatchEvent(new CustomEvent('config-changed', { bubbles: true, composed: true, detail: { config } }));
  }
}

adopt(CARD_TYPE, OptoVScheduleCard);
adopt(EDITOR_TYPE, OptoVScheduleCardEditor);
adopt(FAULT_CARD_TYPE, OptoVFaultHistoryCard);
adopt(FAULT_EDITOR_TYPE, OptoVFaultHistoryCardEditor);

window.customCards = window.customCards || [];
for (const card of [
  {
    type: CARD_TYPE,
    name: 'OptoV Schedule',
    description: 'View and edit the weekly programmes your controller',
  },
  {
    type: FAULT_CARD_TYPE,
    name: 'OptoV Fault History',
    description: "The controller's own fault buffer, decoded",
  },
]) {
  if (!window.customCards.some((c) => c.type === card.type)) {
    window.customCards.push({
      ...card,
      preview: true,
      documentationURL: 'https://github.com/gismo2004/optov',
    });
  }
}
