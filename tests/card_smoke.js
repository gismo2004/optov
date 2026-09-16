#!/usr/bin/env node
/**
 * Renders the schedule card outside a browser and fails on any exception.
 *
 * Why this exists: `node --check` only parses. It happily accepted a `const` used a few lines
 * above its own declaration, which throws at render time; Home Assistant catches that inside
 * setConfig and paints "Configuration error" with nothing useful in any log. That cost a
 * deploy-and-look round trip to find, so the card is now driven here first.
 *
 * The DOM stub is the smallest thing the card touches. It records the rendered HTML and the
 * service calls, so the checks below can assert on what a user would actually see.
 *
 *   node tests/card_smoke.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const CARD = path.join(
  __dirname,
  '..',
  'custom_components',
  'optov',
  'frontend',
  'optov-cards.js'
);
const TRANSLATIONS = path.join(
  __dirname,
  '..',
  'custom_components',
  'optov',
  'translations'
);

const registry = {};

function makeShadowRoot() {
  return {
    innerHTML: '',
    querySelectorAll: () => [],
    querySelector: () => null,
    appendChild: () => {},
  };
}

class FakeElement {
  attachShadow() {
    this.shadowRoot = makeShadowRoot();
    return this.shadowRoot;
  }
  addEventListener() {}
  dispatchEvent() {}
}

const sandbox = {
  HTMLElement: FakeElement,
  customElements: { get: (name) => registry[name], define: (name, cls) => (registry[name] = cls) },
  window: { customCards: [], loadCardHelpers: async () => ({ createCardElement: async () => ({}) }) },
  document: { createElement: () => ({ addEventListener() {}, }) },
  CustomEvent: class {
    constructor(type, options) {
      this.type = type;
      Object.assign(this, options);
    }
  },
  Intl,
  console,
  setTimeout,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(CARD, 'utf8'), sandbox, { filename: CARD });

/** The `card` section of the integration's own translations, as the websocket would return it. */
function cardResources(language) {
  const file = path.join(TRANSLATIONS, `${language}.json`);
  if (!fs.existsSync(file)) return {};
  const card = JSON.parse(fs.readFileSync(file, 'utf8')).card || {};
  return Object.fromEntries(
    Object.entries(card).map(([key, value]) => [`component.optov.card.${key}`, value])
  );
}

const MODES = [
  { value: 0, label: 'Standby', color: 'gray' },
  { value: 1, label: 'Reduziert', color: 'darkblue' },
  { value: 2, label: 'Normal', color: 'darkred' },
  { value: 3, label: 'Festwert', color: 'darkorange' },
];
const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

function scheduleSensor(entityId, name, weekly, extra = {}) {
  return {
    entity_id: entityId,
    state: String(Object.values(weekly).filter((w) => w.length).length),
    last_updated: '2026-09-12T12:00:00Z',
    attributes: {
      schedule: entityId.split('.').pop(),
      name,
      circuit: 'HC1',
      circuit_name: 'Heizkreis 1',
      device_name: 'Vitocal-G mit Vitotronic 200 (Typ WO1A)',
      format: 'phase3',
      windows_per_day: 8,
      minutes_per_step: 10,
      modes: MODES,
      default_level: 0,
      days: DAYS,
      base_address: '0x9200',
      weekly_schedule: weekly,
      ...extra,
    },
  };
}

const weekdayProgramme = [
  { window: 1, start: '04:30', end: '06:00', mode: 2 },
  { window: 2, start: '10:00', end: '16:00', mode: 1 },
];
const weekendProgramme = [{ window: 1, start: '08:00', end: '22:00', mode: 2 }];

const splitWeek = {};
for (const day of DAYS) splitWeek[day] = day === 'sat' || day === 'sun' ? weekendProgramme : weekdayProgramme;
const uniformWeek = Object.fromEntries(DAYS.map((d) => [d, weekdayProgramme]));
const raggedWeek = { ...splitWeek, wed: [{ window: 1, start: '06:00', end: '07:00', mode: 3 }] };
const emptyWeek = Object.fromEntries(DAYS.map((d) => [d, []]));

const serviceCalls = [];

function makeHass(states, language = 'en') {
  return {
    language,
    locale: { language },
    states,
    callWS: async ({ language: wanted }) => ({ resources: cardResources(wanted) }),
    callService: async (domain, service, data) => {
      serviceCalls.push({ domain, service, data });
    },
  };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

async function build(config, states, language) {
  const Card = registry['optov-schedule-card'];
  const card = new Card();
  card.setConfig(config);
  card.hass = makeHass(states, language);
  await tick();
  await tick();
  return card;
}

const failures = [];
function check(name, condition, detail = '') {
  if (condition) {
    console.log(`  ok    ${name}`);
  } else {
    failures.push(`${name}${detail ? ` -- ${detail}` : ''}`);
    console.log(`  FAIL  ${name}${detail ? ` -- ${detail}` : ''}`);
  }
}

async function main() {
  const hk1 = scheduleSensor('sensor.schaltzeiten_hk1', 'Schaltzeiten HK1', splitWeek);
  const ww = scheduleSensor('sensor.schaltzeiten_ww', 'Schaltzeiten WW', uniformWeek, {
    schedule: 'nku_tagesprogramm_ww',
    circuit: 'WW',
    circuit_name: 'Warmwasser',
    base_address: '0x92E0',
  });
  const zp = scheduleSensor('sensor.schaltzeiten_zp', 'Schaltzeiten ZP', emptyWeek, {
    schedule: 'nku_tagesprogramm_zp',
    base_address: '0x9318',
  });
  const states = {
    'sensor.schaltzeiten_hk1': hk1,
    'sensor.schaltzeiten_ww': ww,
    'sensor.schaltzeiten_zp': zp,
    'sensor.flow': {
      entity_id: 'sensor.flow',
      state: '34.5',
      last_updated: '2026-09-12T12:00:00Z',
      attributes: { unit_of_measurement: '°C', friendly_name: 'Flow' },
    },
  };

  console.log('discovery, all programmes');
  let card = await build({}, states);
  let html = card.shadowRoot.innerHTML;
  check('renders something', html.length > 500, `${html.length} chars`);
  check('title is the controller', html.includes('Vitocal-G mit Vitotronic 200 (Typ WO1A)'));
  check('one tab per programme', ['HK1', 'WW', 'ZP'].every((n) => html.includes(`>${n}<`)));
  check('apply-to panel present', html.includes('class="scope"') && html.includes('Apply to'));
  check('level names from the sensor', html.includes('Reduziert') && html.includes('Normal'));

  console.log('a chosen subset');
  card = await build({ entities: ['sensor.schaltzeiten_hk1', 'sensor.schaltzeiten_zp'] }, states);
  html = card.shadowRoot.innerHTML;
  check('only the chosen two', html.includes('>HK1<') && html.includes('>ZP<') && !html.includes('>WW<'));

  console.log('tab names of your own');
  card = await build(
    {
      entities: [
        { entity: 'sensor.schaltzeiten_hk1', name: 'Wohnzimmer' },
        'sensor.schaltzeiten_ww',
      ],
    },
    states
  );
  html = card.shadowRoot.innerHTML;
  check('configured tab name is used', html.includes('>Wohnzimmer<'));
  check('the other tab keeps its own name', html.includes('>WW<'));
  check('the subtitle follows the configured name', html.includes('Wohnzimmer ·'));

  console.log('the editor');
  const Editor = registry['optov-schedule-card-editor'];
  const editor = new Editor();
  editor._config = {
    entities: [{ entity: 'sensor.schaltzeiten_hk1', name: 'Wohnzimmer' }, 'sensor.schaltzeiten_ww'],
  };
  editor._hass = makeHass(states);
  editor._strings = {};
  const schema = editor._schema((k) => k);
  check('a name field per chosen programme', schema.filter((f) => f.programme).length === 2);
  let emitted = null;
  editor.dispatchEvent = (event) => (emitted = event.detail.config);
  editor._onChange({
    stopPropagation() {},
    detail: {
      value: {
        entities: ['sensor.schaltzeiten_ww', 'sensor.schaltzeiten_hk1'],
        tab_0: 'Wohnzimmer',
        tab_1: '',
      },
    },
  });
  check(
    'a name stays with its programme when the order changes',
    JSON.stringify(emitted.entities) ===
      JSON.stringify(['sensor.schaltzeiten_ww', { entity: 'sensor.schaltzeiten_hk1', name: 'Wohnzimmer' }]),
    JSON.stringify(emitted && emitted.entities)
  );

  console.log('tab names while showing every programme');
  card = await build({ names: { 'sensor.schaltzeiten_ww': 'Brauchwasser' } }, states);
  html = card.shadowRoot.innerHTML;
  check('the named tab uses it', html.includes('>Brauchwasser<'));
  check('the others keep their own', html.includes('>HK1<') && html.includes('>ZP<'));

  const editorAll = new registry['optov-schedule-card-editor']();
  editorAll._config = { names: { 'sensor.schaltzeiten_ww': 'Brauchwasser' } };
  editorAll._hass = makeHass(states);
  editorAll._strings = {};
  const allSchema = editorAll._schema((k) => k);
  check('a name field for every programme', allSchema.filter((f) => f.programme).length === 3);
  check(
    'the existing name is prefilled',
    editorAll._tabFields.some((f) => f.entity === 'sensor.schaltzeiten_ww' && f.name === 'Brauchwasser')
  );
  let allEmitted = null;
  editorAll.dispatchEvent = (event) => (allEmitted = event.detail.config);
  const byEntity = Object.fromEntries(editorAll._tabFields.map((f) => [f.entity, f.key]));
  editorAll._onChange({
    stopPropagation() {},
    detail: { value: { entities: [], [byEntity['sensor.schaltzeiten_hk1']]: 'Wohnzimmer' } },
  });
  check('names go to the map, not to a list', !allEmitted.entities && !!allEmitted.names);
  check('the map holds the new name', allEmitted.names['sensor.schaltzeiten_hk1'] === 'Wohnzimmer');

  console.log('legacy single entity');
  card = await build({ entity: 'sensor.schaltzeiten_ww' }, states);
  html = card.shadowRoot.innerHTML;
  check('no programme tabs for one', !html.includes('class="tabs programmes"'));

  console.log('scope preselection');
  const scopeFor = async (weekly, day) => {
    const one = scheduleSensor('sensor.s', 'Schaltzeiten HK1', weekly);
    const c = await build({ entity: 'sensor.s' }, { 'sensor.s': one });
    c._activeDay = day;
    c._scope = null;
    c._render();
    const match = c.shadowRoot.innerHTML.match(/<button class="active" data-scope="(\w+)"/);
    return match && match[1];
  };
  check('uniform week preselects the week', (await scopeFor(uniformWeek, 'mon')) === 'week');
  check('split week preselects weekdays on a weekday', (await scopeFor(splitWeek, 'mon')) === 'weekdays');
  check('split week preselects the weekend on a Sunday', (await scopeFor(splitWeek, 'sun')) === 'weekend');
  check('a ragged week preselects the single day', (await scopeFor(raggedWeek, 'mon')) === 'day');

  console.log('programmed-day marking and matching line');
  card = await build({ entity: 'sensor.schaltzeiten_hk1' }, states);
  card._activeDay = 'mon';
  card._render();
  html = card.shadowRoot.innerHTML;
  // Count the day buttons, not the stylesheet, which also mentions the class.
  const dayButtons = (h) => (h.match(/<button class="tab[^"]*programmed[^"]*" data-day=/g) || []).length;
  check('programmed days are marked', dayButtons(html) === 7, `${dayButtons(html)} marked`);
  check('matching days are named', html.includes('Same as'));
  card = await build({ entity: 'sensor.schaltzeiten_zp' }, states);
  html = card.shadowRoot.innerHTML;
  check('an empty programme marks no day', dayButtons(html) === 0, `${dayButtons(html)} marked`);

  console.log('saving');
  serviceCalls.length = 0;
  card = await build({ entity: 'sensor.schaltzeiten_hk1' }, states);
  card._activeDay = 'mon';
  card._scope = 'weekdays';
  await card._save(states['sensor.schaltzeiten_hk1']);
  check('one service call', serviceCalls.length === 1, JSON.stringify(serviceCalls));
  const call = serviceCalls[0] || { data: {} };
  check('writes the five weekdays', JSON.stringify(call.data.day) === JSON.stringify(DAYS.slice(0, 5)));
  check('sends the programme key', call.data.schedule === 'sensor.schaltzeiten_hk1'.split('.').pop());
  check('levels stay numeric', (call.data.windows || []).every((w) => typeof w.mode === 'number'));
  check('scope resets after the save', card._scope === null);

  console.log('German');
  card = await build({ entity: 'sensor.schaltzeiten_hk1' }, states, 'de');
  html = card.shadowRoot.innerHTML;
  check('German card strings', html.includes('Übernehmen für') && html.includes('Auf Regelung speichern'));
  check('German weekday names', html.includes('Mo') && html.includes('So'));

  console.log('nothing to show');
  card = await build({ entity: 'sensor.missing' }, states);
  html = card.shadowRoot.innerHTML;
  check('missing entity is reported', html.includes('sensor.missing'));

  console.log('fault history card');
  const faults = {
    entity_id: 'sensor.fehlerhistorie',
    state: '20.06.2026 – Neustart der Regelung',
    last_updated: '2026-09-12T12:00:00Z',
    attributes: {
      device_name: 'Vitocal-G mit Vitotronic 200 (Typ WO1A)',
      error_count: 4,
      buffer_address: '0xA801',
      entries: [
        { index: 1, code: 'FF', description: 'Neustart der Regelung', timestamp_str: '20.06.2026 10:20:00' },
        { index: 2, code: 'FF', description: 'Neustart der Regelung', timestamp_str: '06.04.2026 01:27:00' },
        { index: 3, code: 'C9', description: 'Kältekreis', timestamp_str: '29.11.2023 18:17:00' },
        { index: 4, code: 'A9', description: 'Wärmepumpe defekt', timestamp_str: '29.11.2023 18:17:00' },
      ],
    },
  };
  const faultStates = { ...states, 'sensor.fehlerhistorie': faults };
  const FaultCard = registry['optov-fault-history-card'];
  const faultCard = new FaultCard();
  faultCard.setConfig({});
  faultCard.hass = makeHass(faultStates);
  await tick();
  await tick();
  html = faultCard.shadowRoot.innerHTML;
  check('finds the fault sensor by itself', html.includes('Kältekreis') && html.includes('Wärmepumpe defekt'));
  check('shows the controller and the count', html.includes('(Typ WO1A)') && html.includes('4 entries'));
  check('shows each code', html.includes('>C9<') && html.includes('>A9<'));

  const filtered = new FaultCard();
  filtered.setConfig({ hide_codes: ['FF'] });
  filtered.hass = makeHass(faultStates);
  await tick();
  await tick();
  html = filtered.shadowRoot.innerHTML;
  check('hidden codes are dropped', !html.includes('Neustart der Regelung'));
  check('and counted', html.includes('2 hidden'));

  const faultEditor = new registry['optov-fault-history-card-editor']();
  faultEditor._config = {};
  faultEditor._hass = makeHass(faultStates);
  faultEditor._strings = {};
  const faultSchema = faultEditor._schema((k) => k);
  const codeField = faultSchema.find((f) => f.name === 'hide_codes');
  check(
    'the editor offers the codes actually logged',
    codeField.selector.select.options.map((o) => o.value).sort().join(',') === 'A9,C9,FF',
    JSON.stringify(codeField.selector.select.options)
  );
  check(
    'each code carries its text and how often it occurs',
    codeField.selector.select.options.every((o) => /\(\d+\)$/.test(o.label)),
    JSON.stringify(codeField.selector.select.options.map((o) => o.label))
  );
  check('no entity picker when there is only one', !faultSchema.some((f) => f.name === 'entity'));

  const staleEditor = new registry['optov-fault-history-card-editor']();
  staleEditor._config = { entity: 'sensor.fehlerhistorie' };
  staleEditor._hass = makeHass(faultStates);
  staleEditor._strings = {};
  check(
    'still no picker when the config names the only one',
    !staleEditor._schema((k) => k).some((f) => f.name === 'entity')
  );

  check('the list scrolls at a configurable height', faultCard.shadowRoot.innerHTML.includes('max-height: 420px'));
  const tall = new FaultCard();
  tall.setConfig({ height: 900 });
  tall.hass = makeHass(faultStates);
  await tick();
  await tick();
  check('and honours the configured height', tall.shadowRoot.innerHTML.includes('max-height: 900px'));
  const capped = new FaultCard();
  capped.setConfig({ max: 2 });
  capped.hass = makeHass(faultStates);
  await tick();
  await tick();
  check('max limits the rows', (capped.shadowRoot.innerHTML.match(/class="fault"/g) || []).length === 2);

  // Two controllers: now the choice is real and has to be offered.
  const second = JSON.parse(JSON.stringify(faults));
  second.entity_id = 'sensor.fehlerhistorie_2';
  second.attributes.device_name = 'Vitodens 200';
  const twoEditor = new registry['optov-fault-history-card-editor']();
  twoEditor._config = {};
  twoEditor._hass = makeHass({ ...faultStates, 'sensor.fehlerhistorie_2': second });
  twoEditor._strings = {};
  check(
    'two controllers bring the picker back',
    twoEditor._schema((k) => k).some((f) => f.name === 'entity')
  );

  const mostFrequent = codeField.selector.select.options[0];
  check('the most frequent code comes first', mostFrequent.value === 'FF', JSON.stringify(mostFrequent));

  console.log('');
  if (failures.length) {
    console.log(`${failures.length} failed`);
    process.exit(1);
  }
  console.log('all checks passed');
}

main().catch((err) => {
  console.error('threw:', err);
  process.exit(1);
});
