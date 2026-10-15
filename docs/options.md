# Options

| Option | Default | What it does |
|---|---|---|
| Catalog language | German | Names of datapoints, circuits, enumeration values and fault texts. The languages offered are the ones your catalog was built with. |
| Poll interval | 15 s | How often a cycle starts. The base set is read every cycle; slower datapoints rotate through whatever bus time is left. |
| Diagnostics | off | Trending, statistics and the two diagnosis pages |
| Commissioning | off | Installer-level settings |
| Coding 2 | off | Expert coding parameters |
| Expert layer | off | Service-level parameters |
| Keep the clock in sync | on | Correct the controller clock from Home Assistant when it drifts |

Switching a tier on enables its entities about 30 seconds after saving, when Home Assistant
reloads the integration; switching it off disables them again straight away. **Entities you
switched on or off yourself are left alone.** Enable a single datapoint for a dashboard, or
disable one you do not want, and it stays that way through tier changes, restarts and
reinstalls. Everything you have not touched follows the tiers, also after removing and adding
the integration again.

**Statistics of switched-off entities.** Home Assistant keeps the long-term statistics of an
entity a tier switch turned off, so that switching it on again continues its history, and lists
each of them on *Developer tools → Statistics* until deleted. When there is something to delete,
the integration raises a repair under *Settings → Repairs* that offers to delete them all at
once, or to keep them and hide the repair if you expect to switch the tier back on. The same operation is the
service `optov.clear_orphaned_statistics`, see [Services](services.md).

The integration's own text, this options page, entity names it invents and the dashboard cards,
follows the Home Assistant language. Catalog text follows the catalog language above. The two
are independent.

## Custom entities

**OptoV options → Custom entities** builds entities of your own from the controller's values.
They are collected on their own device, *Custom entities*, cost no extra bus time, and leave the
original entities as they are.

**On/off entity.** A value with two named states comes in as a sensor (a reading) or a select (a
setting), because the catalog does not say which of the two names means "on" -- across
controllers that is not even consistent, and some pairs are not on/off at all. *Add on/off
entity* asks for the device, then the value (type to search), then shows everything
preselected: which state is "on", the device class (running, power, problem, ...) and the name.
A reading becomes a binary sensor, a setting a switch that writes the chosen state. *Edit or
remove* in the same menu changes them later.

**Thermostat and water heater.** *Add thermostat* and *Add water heater* assemble a `climate` or
`water_heater` entity from entities that already exist: a setpoint (a number), the operating
mode (a select of this controller), and optionally the current temperature from any temperature
sensor -- a room sensor, for example -- and, for a thermostat, a binary sensor that shows it
heating (an on/off entity made above works). The next step lists the controller's own operating
modes and asks what each one is in Home Assistant (off, heat, auto, ...); modes left on "Not
used" are not offered. Where several share one, for example two that both mean off, you choose
which one is set. Only Home Assistant's own features are used, each mapped to an entity: target
and current temperature, operating mode, on/off from the same mapping, and for a thermostat
whether it is heating. A thermostat can also take **presets**, the buttons Home Assistant shows
as Eco, Comfort, Away and Boost: give each one any switch (a template switch too) or an on/off
setting of the controller, for example party mode for Boost. Choosing a preset turns its entity
on and the other presets off; "None" turns them all off. The new entity shows the states of the
entities it was built from and sets them the same way as when they are changed directly; it does
not regulate anything itself.

### Backup and restore

The custom entities of a controller are kept in one file next to the catalog, which every change
in the dialog rewrites. Its name is built from the ESPHome node's own configuration:

```
custom_<node>_<port>.yaml
```

| Part | Comes from | Example |
|---|---|---|
| `<node>` | the node's name, `esphome:` → `name:` | `heizung` |
| `<port>` | the serial proxy's name, `serial_proxy:` → `name:`; without a name, `port0`, `port1`, ... by its position | `Optolink` → `optolink` |

Both parts are written in lower case, and every run of characters other than letters and digits
becomes a single `_`. The example gives `custom_heizung_optolink.yaml`. The same two names are
what every entity's unique id of this controller starts with.

**The file is deleted together with the integration entry, like the catalog.** *Backup and
restore* offers it for download (the link is valid for ten minutes) and takes a backup to upload
in its place -- whatever the uploaded file is called, it is stored under the name above. A file
from another controller works as a template: entries naming values this controller does not have
are reported and left out. If you edited the file in place, for example with the File editor,
submit without uploading to apply it.

To set up many at once, edit the file. It has one section per kind: an entry in `onoff:` names
the value's entity id (`value`), the raw value that counts as on (`on_value`), and optionally a
`device_class` and a `name`; an entry in `climate:` or `water_heater:` names its `target`,
`mode`, `current` and (thermostat) `heating` entities, and maps the mode select's raw values to
Home Assistant modes under `modes:`, with `write:` saying which value a shared mode sets; a
thermostat's `presets:` maps a preset (`eco`, `comfort`, `away`, `boost`, `home`, `sleep`,
`activity`) to the switch or select that turns it on. The header of the file explains the
format, and each entry's comment says what its two raw values mean. A file that cannot be read
keeps the custom entities you have until it is fixed.

## Entity ids

Entity ids are built from the controller's model code, its circuit and the datapoint, not from
the device's display name. A reinstall therefore lands on exactly the same ids and keeps its
long-term statistics. Two identical controllers get a numeric suffix on the second.
