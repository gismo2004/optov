# CLAUDE.md

Guidance for Claude Code (or any agent) working in this repository.

## What this is

**OptoV**, a Home Assistant custom integration that talks to Viessmann heating controllers over
the Optolink optical port. Installed through HACS; nothing here is a vendor product and nothing
here is endorsed by one. MIT licensed, copyright gismo2004; the README says openly that it was
written together with AI assistants, and that sentence stays.

The catalog it reads is *not* in this repository. It is compiled from the controller service
software by a separate tool, [VExtractor](https://github.com/gismo2004/VExtractor), and every
user builds their own. See "The catalog" below.

The defining property is that **behaviour is driven by the catalog, not by code**. Addresses,
byte order, signedness, bit-field positions, scaling, enumerations, units, fault-code texts,
poll priority, weekly-programme level names and which controller is which all come from catalog
rows. When something needs to vary per controller, the answer is nearly always a catalog column
rather than a branch in Python.

## Layout

```
custom_components/optov/
  __init__.py      setup, registry reconciliation, dashboard resource, services
  optolink.py      P300 transport: framing, checksums, sync, retries, block/array reads
  decode.py        raw bytes -> value, driven by parameter type and conversion
  conversions.py   shared decoders (schedules, BCD timestamps) and their encoders
  catalog_db.py    all catalog access; controller identification; profile generation
  coordinator.py   polling, cadence, per-cycle state, schedules, clock, error history
  profiles.py      the generated entity set as the platforms consume it; stable entity ids
  errors.py        error-history buffer decoding
  sensor.py / binary_sensor.py / number.py / select.py / switch.py   entity platforms
  config_flow.py   setup and options
  diagnostics.py   the download on the entry: controller, catalog, learned state, bus figures
  translate.py     the integration's own UI strings from translations/, for values
  entity.py        what every catalog-driven entity shares
  frontend/        the two dashboard cards (one file)
  translations/    the integration's own text, one file per language
  brand/           the mark and wordmark, SVG sources and PNG renders
hacs.json          HACS metadata
tests/             what runs without hardware: decoder, conversions, faults, the card
docs/              README images of the dashboard cards
work/              git-ignored playground: deployment scripts, scratch data, throwaway probes
```

`brand/` sits **inside** the integration on purpose. Since 2026.9 Home Assistant serves an
integration's own brand images from `<integration>/brand/` before falling back to the brands
CDN, so the icon in the integrations list, the config flow and the device page all work without
anything being accepted upstream. It is the presence of the directory that switches this on
(`Integration.has_branding`), and only these exact names are served: `icon.png`, `logo.png`,
`icon@2x.png`, `logo@2x.png` and the four `dark_` variants. Sizes follow the brands convention,
icons square at 256 and 512, logos bounded by 512 and 1024 on the long side, which also makes
the set ready to submit upstream unchanged.

The mark is ours: the round optical window on the front of a controller, dark glass in a metal
ring, with a beam of light converging inside it; the two rays make the V. Navy `#16213A` for the
housing, amber `#F59E0B` for the light, Montserrat Bold for the word. It borrows nothing from any
manufacturer's identity and must not start to.

Four SVG sources, eight PNGs. `logo.svg` embeds the mark from `icon.svg`, and the `dark_`
variants lighten the housing so the disc still reads on a dark theme. Edit the SVGs and
re-render with `rsvg-convert` (Montserrat installed); never edit a PNG. Change `icon.svg` first,
then rebuild the logo from it, and keep the dark pair in step.

Nothing in the repository outside `work/` may reference anything inside it.

## The name

**OptoV**, written with a capital O and a capital V, everywhere it is shown to a person: the
README, the integration's display name, the card picker, the repository. Lowercase `optov` only
where an identifier demands it -- the Home Assistant domain, the config directory
`<config>/optov/`, entity ids, the card element names `optov-schedule-card` and
`optov-fault-history-card`.

The manufacturer's name is never part of the product name. It belongs in the repository
description, the topics, and prose that says which equipment this talks to, which is referential
use and is what keeps the project findable in HACS. Never a logo, never "official", and the
disclaimer at the top of the README stays there.

## The catalog

**No catalog is committed, in any form, under any name.** It is derived from the vendor's
parameter definitions. `.gitignore` refuses `*.db`; keep it that way.

At runtime a catalog lives in `<config>/optov/`, outside the integration's own directory,
because an update replaces that directory wholesale. The file is uncompressed and its name is
the user's own. Several may sit there and **each config entry records the one it was set up
with**, in `CONF_CATALOG`. No code may assume a single fixed catalog path: `get_db_connection()`
takes one and has no default, and the coordinator carries `self.db_path` from the entry.

**Reconfigure** is one screen, not a menu: a flow has no way back from a second screen. It
changes `CONF_CATALOG` on the entry (choose another file or upload a new one) and offers a
checklist of catalogs to delete. The checklist shows every catalog, with the ones an entry
records, in any state, disabled included, marked as in use and refused on submit; Home
Assistant's list selector cannot grey an option out. Changing the catalog updates the entry and
stops; the entry's update listener reloads, because it
compares the catalog the entry started with (`runtime_data.catalog`) as well as the options. Do
not replace that with `async_update_reload_and_abort`: with an update listener present Home
Assistant logs a usage warning for it and has announced it will stop working. Only an entry
without a listener (setup failed) or an unchanged name after a re-upload is reloaded explicitly.

Because Reconfigure refuses a catalog its own entry uses, the last entry's catalog could never be
deleted from within Home Assistant. `async_remove_entry` closes that: removing an entry deletes
its catalog unless another entry records the same file. Home Assistant still lists the entry
being removed when it calls that hook, so it is excluded by id.

Nothing module-level in `catalog_db` may cache what a catalog contains without keying it to the
catalog file, since an entry can switch files at runtime and several entries can use different
ones.

`catalog_db.CATALOG_SCHEMA_VERSION` is the structure this code is written against, and the
compiler writes its own number into the catalog's `catalog_meta` table. They are checked on
upload and on every setup, in both directions, so an old catalog under a new integration and a
new catalog under an old one each get a message naming the side that is behind. Raise it only
together with the compiler's constant, and only when an older catalog would actually be wrong.

The *Catalog* diagnostic sensor on the gateway device shows that structure version as its state,
with the file name, controller count and languages as attributes (`catalog_db.catalog_info`).

## Device names

Every device is named after **what it is**, not after the product: `Controller`, `Warmwasser`,
`Heizkreis 1`, `Optical interface`. The controller and the bridge take theirs from
`translations/<lang>.json` under `device`, the circuits from the catalog's own circuit labels.

The product name is the device `model`, and the variant code is `model_id`, which is what those
two fields are for. It is also the config entry's title, above all of them. It must not become
a device name again: Home Assistant builds a card's label from the device chain, so the name of
the device an entity hangs off is repeated on every one of its entities. Putting the product
name back on the controller would prefix the several hundred entities that sit directly on it.

## Identity

Every entity's unique id and every device identifier is prefixed with
`OptolinkCoordinator.stable_id`, which is the ESPHome node name and the serial proxy name, both
written by hand in the node's configuration. **Nothing derived from the config entry may go into
an identity.** Home Assistant keeps a removed entity for thirty days and restores its enabled
state, area, custom name, hidden flag and labels when the same unique id reappears; a config
entry id is regenerated on every re-add, so using it discarded all of that silently. The MAC
address was rejected for the same class of reason: it does not survive replacing the hardware,
whereas a name in a configuration file does.

`_async_migrate_identity` moves an installation off the old prefix, once. Leave it in place.

Two entries must not produce the same stable id. The config flow already refuses a proxy that
another entry uses, and two nodes cannot share a name on one network.

## Ground rules

**Comments explain behaviour and rationale, never history.** Say what the protocol or the
catalog does and why the code responds that way. No "we discovered", no war stories, no names of
tools that happened to be on the desk when a fact was established; that kind of narrative
belongs in a changelog or a notes file, not in code that other people will read. Catalog tables,
columns and text keys are fine to name; they are ours.

**Vendor naming belongs in the catalog, not in this code.** Where the shipped code once built
the source's own text keys, it now reads a finished key out of a catalog column. Product names,
fault texts and level names are data. The manufacturer's name appears deliberately in exactly
two kinds of place: `manufacturer=` on the device, and prose that tells a person which equipment
is meant. Nowhere else, and never as a fallback value or an identifier.

**No hardware, network or deployment specifics.** No IP addresses, API keys, hostnames or
personal paths anywhere outside `work/`. Examples stay generic.

**No code names a language.** Two translation layers, both file-driven: catalog text (datapoint,
circuit, enumeration, fault and schedule-level names) comes from the compiled catalog in the
language chosen in the options; the integration's own text lives in `translations/<lang>.json`
and follows the Home Assistant language, via `translation_key` on entities and devices, the
`card` and `text` sections, and `services.yaml` selectors. Adding a language is adding one file.
An `if language == "de"` branch anywhere is a bug.

**Ask the device, don't guess.** Where the catalog is silent or self-contradictory, prefer a
one-off probe at startup over an assumption: the controller is the authority on what it
implements, what read lengths it accepts, and how its array datapoints are addressed. Several
such probes already exist (`calibrate_record_step`, the unsupported-address set, the equipment
probe) and they follow that pattern deliberately.

**Measure before optimising, and record negative results.** Several plausible ideas here turned
out to be wrong when measured, and the reasons are written next to the code so they are not
re-tried. If you find one of those notes, take it seriously; if you disprove one, replace it.

## Things that are easy to get wrong

- **Read the block, not the field.** A datapoint's block length is the telegram the controller
  expects at that address; its byte length is the field you want from inside it. Requesting the
  field length when the two differ makes the controller reject the read.
- **Where the catalog states no limits for a setting, the datapoint says what they are.** Its
  parameter type's width and sign give the range and the conversion's divisor the step
  (`decode.raw_bounds`, `catalog_db._number_limits`). Falling back to a made-up range instead
  refused values the controller holds happily -- more than a third of a family's settings carry
  no limits at all -- and a whole-number step hides the tenths a scaled datapoint is set in.
- **Bit-field numbering is MSB-first within each byte**, across the whole block, not a
  little-endian shift-and-mask.
- **Signedness follows the declared parameter type width**, not the datapoint's byte count, and
  unsigned types are not masked at all.
- **A System ID is not an identity.** Many controllers share one; identification needs the
  hardware and software indices too, and occasionally one extra register.
- **The catalog is a superset.** It describes a controller family, so it lists datapoints a
  given unit does not have. That is expected, not an error.
- **One telegram per datapoint, and do not try to be clever about it.** Reading a range that
  spans several datapoints and cutting it up by address is not safe here: the controller accepts
  such a read and answers with bytes that are not what those addresses return individually, in
  some regions but not others, with no catalog column telling them apart and no error when you
  get it wrong, just plausible, silently shifted values. This was tried, measured and withdrawn;
  the saving was 22 % of one poll cycle for two hundred lines of risk.
- **`with sqlite3.connect(...)` does not close the connection.** It ends the transaction and
  nothing else. Every catalog query goes through `contextlib.closing`, and a connection opened
  that way must not be used past the end of its block.
- **The serial port is looked up by the node's address, not taken from the entry.** The URL of
  a proxy on a node Home Assistant has names that node's config entry, and setting the node up
  again gives it a new id. `_port_url` resolves it from `CONF_HOST` and the proxy's position at
  every start and writes the result to `CONF_DEVICE`, the key Home Assistant reads to show who
  holds a port. A node it does not have keeps the `esphome://` URL it was set up with.
- **The page a browser has is older than Home Assistant thinks.** Its service worker serves
  the app shell from its own cache until Home Assistant's frontend is updated, so anything
  written into that page -- `frontend.add_extra_js_url` among it -- reaches nobody whose shell
  predates it, for weeks. The dashboard resource list does not travel that way: the frontend
  fetches it over the websocket on every load. Hence the cards go into the resource list where
  there is one, and into the page only for a YAML dashboard, which has no list. The file is
  served from `async_setup`, not from an entry: a dashboard asking for it while Home Assistant
  is still starting, or while a controller is unreachable, would otherwise get a 404 -- and a
  browser keeps a 404 for that URL. What frees it is a new `?v=`, which is the card file's own
  timestamp, so an update writing fresh files moves it by itself.
- **Who enabled an entity lives on the entity, not in the entry.** Home Assistant records no
  author for enabling or disabling, and restores a removed entity's enabled state and registry
  options when it is re-added, but not the config entry. `_async_track_manual_changes` marks an
  entity `manual` in its registry options when a person switches it; `_async_apply_enabled_states`
  leaves marked and user-disabled entities alone and sets everything else from the tiers, before
  the platforms and again after them for restored entities. Enabling an entity from code must go
  through `OWN_ENABLES_KEY`, or the listener takes it for a person's switch.
- **An options change that switches entities on does not reload from the update listener.**
  Home Assistant reloads an entry 30 seconds after one of its entities is enabled, whoever
  enabled it, so `_async_options_updated` enables them for the new options
  (`async_profile_for_options`) and leaves the reload to that. Reloading there as well is what
  used to reload twice. Everything else (a tier off, language, interval, catalog) reloads at
  once, because disabling triggers no reload of Home Assistant's.

## Working on the integration

`tests/` holds what can be checked without hardware, and CI runs it on every push: the decoder,
the conversions and the fault buffer are free of Home Assistant imports on purpose, so they are
imported directly and tested against the rules the catalog implies -- signedness by declared
width, MSB-first bit fields, the round trip of every schedule layout. Everything above them --
the transport's timing, the catalog queries, the coordinator -- still needs a real controller,
and the probe scripts for that live in `work/`.

`node tests/card_smoke.js` renders the dashboard card against a stub DOM and a fake Home
Assistant and fails on any exception. Run it after every card change. `node --check` only
parses, and a runtime error in the card surfaces in the browser as "Configuration error" with
nothing in any log.

Home Assistant does not re-import Python on a config-entry reload; a full restart is required to
pick up code changes.

Before finishing, run `python3 -m pyflakes custom_components/optov/*.py`. It catches the
undefined name left behind by an edit that removed too much, which nothing else here would.
