# Changelog

What changed for someone using OptoV, release by release. Internal work -- refactoring,
formatting, tooling -- is not listed; the commit history has it. Versions follow semantic
versioning and stay at 0.x while the integration is in beta.

The **Unreleased** section is written as the changes are made, one line per change, so that a
release is a matter of giving the section a number rather than of reconstructing weeks of
commits.

## [Unreleased]

### Changed
- The bridge no longer has to be an ESP32: any device running ESPHome's
  [serial proxy](https://esphome.io/components/serial_proxy/) works.
- The bridge device is now called "Optolink Bridge", with no protocol or board it may not have.

### Fixed
- A setting changed while a poll is running no longer jumps back to its old value for one cycle.
- Two settings sharing one register, changed at the same moment, no longer undo each other.
- A programme edit made while that programme is being re-read in the background is no longer
  undone.

## [0.4.0] - 2026-09-23

### Changed
- Entities that share a name can now be told apart. The catalog often names a relay's state, its
  switching counter and its running hours identically -- "Sekundärpumpe 1" three times. The live
  reading keeps the plain name; the others get the controller's own menu page appended, such as
  "Sekundärpumpe 1 · Schaltzyklen WP". On/off readings now show a toggle icon and switching
  counters a counter icon instead of the same default eye. Entity ids do not change, and names
  you have set yourself are kept.

## [0.3.0] - 2026-09-22

### Added
- Settings now explain themselves. Every entity carries the controller's own description of
  its datapoint as an attribute, so opening a coding parameter in Home Assistant tells you
  what changing it will do, not just what it is called. The text was already in the catalog
  and is kept out of the recorder database.

## [0.2.1] - 2026-09-22

### Fixed
- Weekly programmes stayed empty on some controllers. Where it happened, every programme
  card was blank, there was nothing to switch or edit, and the log repeated `Could not
  refresh programme ...` for all of them on every poll -- fifteen warnings a cycle, for as
  long as the integration ran. Reported on a Vitocal 333-G (#3); a Vitocal 200 with the same
  catalog entry was never affected, which is what took a while to explain.

  The cause was how a programme is asked for. A programme is an array of records, too large
  for one telegram, and controllers disagree about how many records may be fetched at once:
  some serve any number up to the telegram limit, some accept exactly one. OptoV assumed the
  first kind, read the refusal as a fault of its own and gave up on the programme, then tried
  again on the next poll, forever. It now falls back to fetching one record at a time the
  moment a controller objects, remembers that for the rest of the session, and only treats a
  programme as genuinely absent when even a single record is refused.

  After updating, the programmes appear by themselves within a poll cycle; nothing needs to
  be re-added or reconfigured. Controllers that were already working keep the fast path and
  gain a little: three telegrams fewer per programme at startup.

## [0.2.0] - 2026-09-21

### Added
- The setup flow can ask the controller which controller it is. When no usable catalog is
  present -- the position every new user starts from -- it offers to read DeviceIdent over the
  chosen port and reports the system id, which is what a catalog is built for, before asking for
  the catalog. The read also consults register 0x00F0 where the identification extension calls
  for it, so a catalog is not built for the wrong variant. Skippable throughout; nothing about
  an existing setup changes. A controller speaking a protocol OptoV does not drive is named
  as such instead of being offered a catalog. Closes #2.
- The catalog step of the setup and reconfigure flows now names the catalog structure version
  the integration wants, and the oldest it still reads. The version of each catalog was already
  shown beside its name, but there was nothing to compare it against without leaving the form.
- A second fault-history sensor on boilers with a burner automat: the automat's own fault
  records, with the texts of the automat that is fitted. The texts need a catalog of structure
  version 2, built with VExtractor 0.1.3 or newer; an older catalog shows the bare codes.
  Untested on hardware so far.
- A repair that says when the catalog an entry runs on is older than the structure this
  version is written for. Such a catalog keeps working, minus what was added since; the
  repair names the version and goes away once the entry starts on a rebuilt one. A catalog
  the integration cannot read at all still fails setup with the message it always had.
- A repair that offers to delete the long-term statistics of entities a tier switch turned
  off, which Home Assistant otherwise lists one by one on its statistics page, or to keep
  them and hide the repair. The same
  operation is the service `optov.clear_orphaned_statistics`, which can also take entities
  disabled by hand.

### Fixed
- Read-only single-bit readings are entities again: relay states (the electric heater's
  stages, the heating/hot-water valve), the device status flags (heating period, party, eco,
  holiday, frost protection) and the digital inputs of the diagnosis page. They were dropped
  by the rule that folds a value's sensor-health nibble into the value, which now only applies
  to a bit-field that shares its address with a full-width value. On a Vitotronic 200 WO1A this
  adds 52 sensors, 13 of them on by default because they sit on the controller's overview page.

### Changed
- KW is no longer marked experimental: a user confirmed it on a Vitotronic 200 KW2 (0x2098) with
  the full entity set (#1). The code is unchanged; the README and `docs/protocols.md` say so.
- The controller-clock sensor now reports the clock's drift from Home Assistant in seconds, on
  a five-second grid, instead of the time it shows. The time was a new state every poll and
  the single largest source of recorder rows; the drift is what matters and rarely changes.
- The bus diagnostics (duty cycle, sweep duration, round trip, datapoint rate) publish on a
  coarse grid and without per-sweep counters in their attributes, so they only write a
  recorder row when something changed. The exact figures are in the diagnostics download.

## [0.1.0] - 2026-09-17

The first tagged release. Verified end to end on one controller, a Vitocal heat pump with a
Vitotronic 200 WO1A, over P300.

### Added
- Experimental support for controllers that speak only the older KW protocol, chosen
  automatically. Untested on hardware; see `docs/protocols.md`.
- Controllers this integration cannot drive over Optolink -- the GWG families, the two-wire bus
  family and the OpenTherm entries -- are refused with a message saying what they speak instead.
- A diagnostics download on the integration entry.
- Documentation pages under `docs/`; the README is the short version.

### Changed
- A setting the catalog states no limits for takes its range and step from the datapoint's
  own type and scaling, instead of a made-up 0-100.
- A controller that never answers is reported as unreachable, not as a missing catalog entry.
- A controller whose hardware index the catalog never saw is still placed by its software
  index when every variant of its System ID shares one hardware index.

[Unreleased]: https://github.com/gismo2004/optov/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/gismo2004/optov/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/gismo2004/optov/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/gismo2004/optov/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/gismo2004/optov/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gismo2004/optov/releases/tag/v0.1.0
