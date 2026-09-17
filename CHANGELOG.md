# Changelog

What changed for someone using OptoV, release by release. Internal work -- refactoring,
formatting, tooling -- is not listed; the commit history has it. Versions follow semantic
versioning and stay at 0.x while the integration is in beta.

The **Unreleased** section is written as the changes are made, one line per change, so that a
release is a matter of giving the section a number rather than of reconstructing weeks of
commits.

## [Unreleased]

### Added
- A repair that offers to delete the long-term statistics of entities a tier switch turned
  off, which Home Assistant otherwise lists one by one on its statistics page. The same
  operation is the service `optov.clear_orphaned_statistics`, which can also take entities
  disabled by hand.

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
