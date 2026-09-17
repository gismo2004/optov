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

The integration's own text, this options page, entity names it invents and the dashboard cards,
follows the Home Assistant language. Catalog text follows the catalog language above. The two
are independent.

## Entity ids

Entity ids are built from the controller's model code, its circuit and the datapoint, not from
the device's display name. A reinstall therefore lands on exactly the same ids and keeps its
long-term statistics. Two identical controllers get a numeric suffix on the second.
