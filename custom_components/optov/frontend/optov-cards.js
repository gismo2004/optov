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
  .timeline { margin: 12px 0; }
  .bar { position: relative; height: 38px; border-radius: 6px; overflow: hidden; background: var(--secondary-background-color); }
  .segment { position: absolute; bottom: 0; display: flex; align-items: center; justify-content: center;
             font-size: 0.7rem; font-weight: 500; color: var(--text-primary-color, #fff); overflow: hidden; white-space: nowrap;
             background: var(--primary-color); border-radius: 3px 3px 0 0; }
  .ticks { display: flex; justify-content: space-between; font-size: 0.72rem; color: var(--secondary-text-color); margin-top: 4px; }
  .matching { font-size: 0.8rem; color: var(--secondary-text-color); margin: -6px 0 10px; }
  .scope { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 12px;
           padding: 10px 12px; border-radius: 10px; background: var(--secondary-background-color); }
  .scope .label { font-size: 0.9rem; font-weight: 500; margin-right: 4px; }
  .scope button { padding: 7px 14px; border-radius: 16px; border: 1px solid var(--divider-color);
                  background: var(--card-background-color); color: var(--secondary-text-color);
                  font: inherit; font-size: 0.88rem; cursor: pointer; white-space: nowrap; }
  .scope button:hover { color: var(--primary-text-color); border-color: var(--primary-color); }
  .scope button.active { background: var(--primary-color); border-color: var(--primary-color);
                         color: var(--text-primary-color, #fff); font-weight: 500; }
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
  .footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-top: 12px;
            border-top: 1px solid var(--divider-color); }
  .status { font-size: 0.9rem; color: var(--secondary-text-color); min-width: 0; }
  .status.ok { color: var(--success-color, var(--primary-color)); }
  .status.error { color: var(--error-color); }
  .actions { display: flex; gap: 8px; flex: none; }
  .button { padding: 9px 18px; border-radius: 8px; border: 1px solid var(--divider-color); background: none;
            color: var(--primary-text-color); font: inherit; font-weight: 500; cursor: pointer; }
  .button.primary { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .button:disabled { opacity: 0.5; cursor: default; }
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
    this._signature = null;
    this._strings = null;
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
    for (const ent of this._entities()) parts.push(ent.entity_id, ent.last_updated);
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

  async _save(entity) {
    const t = makeTranslator(this._strings || {});
    const lang = languageOf(this._hass);
    const windows = this._windowsOf(entity)
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
    const scope = this._scope || detectScope(dayKeys, this._activeDay, attrs.weekly_schedule || {});
    const targets = scopeDays(scope, dayKeys, this._activeDay);
    const shortNames = weekdayNames(lang, 'short');
    const longNames = weekdayNames(lang, 'long');

    this._saving = true;
    this._status = null;
    this._render();
    try {
      await this._hass.callService(DOMAIN, 'set_schedule_day', {
        schedule: attrs.schedule,
        day: targets,
        windows: windows.map((w, i) => ({ window: i + 1, start: w.start, end: w.end, mode: w.mode })),
      });
      // Every target day now holds what was just written, so no pending edit of one can
      // still be meaningful.
      for (const day of targets) delete this._edits[`${entity.entity_id}|${day}`];
      this._scope = null; // null = follow the pattern the week is already in
      this._status = {
        kind: 'ok',
        text: t('saved', {
          day: scopeLabel(scope, dayKeys, this._activeDay, shortNames, longNames, t),
        }),
      };
      setTimeout(() => {
        if (this._status && this._status.kind === 'ok') {
          this._status = null;
          this._render();
        }
      }, 4000);
    } catch (err) {
      this._status = { kind: 'error', text: t('save_failed', { err: (err && err.message) || err }) };
    } finally {
      this._saving = false;
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
    const canSave = dirty || scope !== 'day';

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
      <style>${STYLE}</style>
      <ha-card>
        <div class="header">
          <ha-icon icon="mdi:calendar-clock"></ha-icon>
          <div class="heading">
            <div class="title">${escapeHtml(title)}</div>
            <div class="subtitle">${escapeHtml(subtitle)}</div>
          </div>
        </div>

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

        <div class="tabs days">${dayKeys
        .map((d) => {
          const active = d === this._activeDay ? 'active' : '';
          const isDirty = this._isDirty(entity, d) ? 'dirty' : '';
          const has = programmed.has(d) ? 'programmed' : '';
          return `<button class="tab ${active} ${isDirty} ${has}" data-day="${d}" title="${escapeHtml(longNames[d] || d)}">${escapeHtml(shortNames[d] || d)}</button>`;
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

        <div class="scope">
          <span class="label">${escapeHtml(t('apply_to'))}</span>
          ${SCOPES.map((value) => {
        const active = value === scope ? 'active' : '';
        const label = scopeLabel(value, dayKeys, this._activeDay, shortNames, longNames, t);
        return `<button class="${active}" data-scope="${value}">${escapeHtml(label)}</button>`;
      }).join('')}
        </div>

        <div class="footer">
          <div class="status ${this._status ? this._status.kind : ''}">
            ${escapeHtml(this._status ? this._status.text : dirty ? t('unsaved') : '')}
          </div>
          <div class="actions">
            ${dirty && !this._saving ? `<button class="button discard">${escapeHtml(t('discard'))}</button>` : ''}
            <button class="button primary save" ${this._saving || !canSave ? 'disabled' : ''}>
              ${escapeHtml(this._saving ? t('saving') : t('save'))}
            </button>
          </div>
        </div>
      </ha-card>`;

    this._bind(entity, levels, step);
  }

  _bind(entity, levels, step) {
    const root = this.shadowRoot;
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
      this._status = null;
      this._render();
    });

    on('.save', 'click', () => this._save(entity));
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
      <style>${FAULT_STYLE}</style>
      <ha-card>
        <div class="header">
          <ha-icon icon="mdi:history"></ha-icon>
          <div class="heading">
            <div class="title">${escapeHtml(this._config.title || attrs.device_name || t('faults_title'))}</div>
            <div class="subtitle">${escapeHtml(subtitle)}</div>
          </div>
        </div>
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
  }

  setConfig(config) {
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
  }

  setConfig(config) {
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

if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, OptoVScheduleCard);
}
if (!customElements.get(EDITOR_TYPE)) {
  customElements.define(EDITOR_TYPE, OptoVScheduleCardEditor);
}
if (!customElements.get(FAULT_CARD_TYPE)) {
  customElements.define(FAULT_CARD_TYPE, OptoVFaultHistoryCard);
}
if (!customElements.get(FAULT_EDITOR_TYPE)) {
  customElements.define(FAULT_EDITOR_TYPE, OptoVFaultHistoryCardEditor);
}

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
