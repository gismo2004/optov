# Changelog

What changed for someone using OptoV, release by release. Internal work -- refactoring,
formatting, tooling -- is not listed; the commit history has it. Versions follow semantic
versioning and stay at 0.x while the integration is in beta.

The **Unreleased** section is written as the changes are made, one line per change, so that a
release is a matter of giving the section a number rather than of reconstructing weeks of
commits.

## [Unreleased]

### Added
- The setup flow can ask the controller which controller it is. When no usable catalog is
  present -- the position every new user starts from -- it offers to read DeviceIdent over the
  chosen port and reports the system id, which is what a catalog is built for, before asking for
  the catalog. The read also consults register 0x00F0 where the identification extension calls
  for it, so a catalog is not built for the wrong variant. Skippable throughout; nothing about
  an existing setup changes. Closes #2. Untested on hardware so far.
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

[Unreleased]: https://github.com/gismo2004/optov/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/gismo2004/optov/releases/tag/v0.1.0
