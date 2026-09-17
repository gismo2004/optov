# The catalog

The catalog describes what your controller exposes: addresses, byte layouts, scaling,
enumerations, fault texts, menu structure and weekly programmes. It is derived from the
controller service software's own definitions, so it is not distributed here, and the
integration cannot be set up without one.

**Build it with [VExtractor](https://github.com/gismo2004/VExtractor).** Download that tool,
run it, answer two questions. Its README has step-by-step instructions for Windows and Linux. It
takes a few minutes, once.

What comes back is a single `.db` file. Adding the integration asks you to upload it and puts it
in place itself, rejecting anything that is not a catalog. It lands in `<config>/optov/`, outside
the integration's own directory on purpose: an update replaces that directory wholesale and a
catalog kept inside it would be deleted.

You can keep more than one, under any names you like, for example one per controller. Setup asks
which to use as soon as there is a choice and remembers that choice with the entry; with a single
catalog it does not ask at all.

**Changing it later.** *Reconfigure* on the integration entry picks another catalog or uploads a
newer build, also when the entry failed to start because of its catalog. An upload under an
existing name replaces that file for every controller using it, and catalogs no controller uses
can be deleted there too. Removing an entry deletes its catalog as well, unless another entry uses
it, so keep your copy of the file. The *Catalog* sensor on the Optical interface device shows
which file and version an entry runs on.

A catalog records the structure it was built to, and the integration checks it on every start.
If an update needs a newer one you are told to rebuild; if a catalog is newer than the
integration you are told to update instead. Neither happens silently and there is nothing to
migrate.

Addresses, byte order, signedness, bit positions, scaling, enumerations, units, fault texts and
menu structure are all rows in the catalog, not code. That is what lets a whole controller family
work without a code change, and it is why a wrong value is fixed in VExtractor rather than here.
