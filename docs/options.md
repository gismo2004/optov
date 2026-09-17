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

## Entity ids

Entity ids are built from the controller's model code, its circuit and the datapoint, not from
the device's display name. A reinstall therefore lands on exactly the same ids and keeps its
long-term statistics. Two identical controllers get a numeric suffix on the second.
