# Protocols

Controllers speak one of two protocols over the Optolink port, and OptoV works out which one
yours is: it says hello, and what comes back decides. Nothing to configure.

**P300** is what the verified controller speaks and what everything here is built around.

**KW** is the older protocol, the only one the earliest Vitotronic controllers have -- among
others the Vitotronic 200 KW1/KW2 and 300 KW3. The [openv wiki](https://github.com/openv/openv/wiki/Ger%C3%A4te)
lists which controllers those are.

> **KW support is highly experimental and has never touched a physical controller.** No unit
> that speaks it was available to test against: the telegrams are covered by unit tests and the
> protocol is chosen automatically, but nobody has yet seen a single real reading come back over
> it. If you have such a controller, please try it and
> [open an issue](https://github.com/gismo2004/optov/issues) with the log either way -- that is
> the only way it stops being experimental.

Two things the KW protocol cannot do, which you may notice if you have one of these controllers:

- **It has no way to refuse a read.** Where a P300 controller answers "I do not have that
  address", a KW controller either says nothing or answers with every bit set -- both were seen
  on a controller that speaks both protocols, which returned `ff ff` for an address it refuses
  outright over P300. Neither can be told from a reading of -0.1, so neither is published as a
  value: the entity is unavailable instead. An address that gives nothing usable for twenty
  cycles, while the rest of the bus answers, stops being asked **for that session only** -- the
  next restart tries it again, because a protocol that cannot say no never gives certainty
  enough to disable an entity for good. The catalog's own installation rules stay the real
  filter, and the first minutes after a start are slower than the rest.
- **It has no remote procedure call.** Datapoints only reachable that way get no entity, since
  it could never show a reading: on a Vitotronic 050 HK1W that is 16 of them, on a Vitotronic
  200 KW1/KW2 none at all -- the ones affected there are commands that get no entity anyway. The
  heat-pump families read their fault history that way, but those controllers speak P300, so
  their fault history is unaffected.

Everything else is the same either way: the same catalog, the same entities, the same cards and
services. Which protocol is in use is in the integration's diagnostics download.

**A third protocol, GWG**, runs on the oldest wall-hung gas units -- System IDs 0x2053 and 0x2054,
the Vitodens 100/200 generation with a VR20 board -- and addresses memory with a single byte.
OptoV does not speak it and refuses those controllers by name rather than trying: a two-byte
address sent to them would not be refused, it would land somewhere else. The same refusal covers
the two-wire bus family (0x2000) and the OpenTherm entries (0x2621, 0x26FF), whose datapoints are
OpenTherm data-ids rather than addresses. All of them are in the catalog because they come from
the same source as everything else; being described there does not make them reachable.
