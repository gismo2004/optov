<p align="center">
  <img src="https://raw.githubusercontent.com/gismo2004/optov/main/custom_components/optov/brand/logo.png"
       alt="OptoV" width="420">
</p>

# OptoV

A Home Assistant integration for Viessmann heating controllers, heat pumps and boilers, over
the Optolink optical service port, using an ESP32 running stock ESPHome as the bridge.

> **Not affiliated with Viessmann.** This is an independent project, built and maintained by
> the community. It is not endorsed by, sponsored by or connected with Viessmann in any way, and
> nothing here is official. Viessmann, Vitocal, Vitotronic, Vitodens and Optolink are trademarks
> of their respective owners and appear here only to say which equipment this software talks to.
> No warranty of any kind; you use it at your own risk, on your own equipment.

It is **catalog-driven rather than hand-maintained**. Instead of a curated list of memory
addresses per model, it reads a catalog describing your controller, works out which unit it is
actually talking to, and builds the entity set for that one: names, units, scaling,
enumerations, fault texts and weekly programmes all come from the catalog. The catalog is not
part of this repository; you build it yourself, see [The catalog](#the-catalog).

> **Status: beta.** One controller, a Vitocal heat pump with a Vitotronic 200 WO1A, has been
> verified end to end on real hardware, with Home Assistant 2026.9 and ESPHome 2026.8. Others
> are described by the same definitions and should work the same way, but none has been
> confirmed on a physical unit. Reports are welcome, whether they work or not.

## What you need

| | |
|---|---|
| Controller | A Viessmann controller with an Optolink port (the round optical window on the front). Vitotronic 200/300 families, Vitocal heat pumps, Vitodens and Vitocrossal boilers among others. |
| Optolink adapter | An IR read/write head for that port. Self-built adapters are common; anything that presents the port as a 4800 baud, 8 data bits, even parity, 2 stop bits serial line works. |
| Bridge | An ESP32 running **stock ESPHome 2026.3 or newer** with the built-in `serial_proxy` component (still marked experimental by ESPHome). No custom firmware component is needed: the ESP relays raw bytes and nothing else, the whole protocol lives in Home Assistant. |
| Home Assistant | **2026.5 or newer**, with the ESPHome integration set up for that node. Earlier releases ship an ESPHome client library without the serial proxy calls this integration uses. |
| Catalog | Built once for your controller with [VExtractor](https://github.com/gismo2004/VExtractor), see [The catalog](#the-catalog). |

## Installation

**Through HACS** (recommended). Add this repository as a custom repository of type
*Integration*, install *OptoV*, and restart Home Assistant.

**By hand.** Copy `custom_components/optov/` into your Home Assistant
`config/custom_components/` directory and restart.

Then add the integration under **Settings → Devices & services → Add integration**. Every
serial proxy on every ESPHome node is offered, so pick the one wired to the controller. Nothing
in the ESPHome API says which port that is, so the choice is yours; a wrong one simply fails to
start with "controller not reachable". Ports already used by another entry are not offered,
because a serial proxy serves exactly one client. If the node's API key is not stored on its
ESPHome entry you are asked for it. Setup then asks you to upload the catalog.

## The catalog

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

## ESPHome configuration

A minimal node. Adjust the pins to your adapter; the line settings are what the port expects.

```yaml
esphome:
  name: optolink

esp32:
  board: esp32dev

api:
  encryption:
    key: !secret api_encryption_key

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

uart:
  id: optolink_uart
  tx_pin: GPIO17
  rx_pin: GPIO16
  baud_rate: 4800
  data_bits: 8
  parity: EVEN
  stop_bits: 2

serial_proxy:
  - id: optolink
    name: "Optolink"
    port_type: TTL
    uart_id: optolink_uart
```

Keep the adapter off the ESP32's `TX`/`RX` pins (GPIO1 and GPIO3). The ESP32 prints its boot
messages there on every restart, before ESPHome takes over, and with the adapter on those pins
they go straight into the controller's optical port.

The serial proxy serves **one client at a time**. Do not point a second tool at the same port
while Home Assistant is connected; both will see garbage. A single ESPHome node may expose
several ports, and each is discovered and configured as an independent integration entry in
Home Assistant. With two controllers on one node, give each proxy a name of its own:

```yaml
serial_proxy:
  - id: optolink_eg
    name: "EG"
    port_type: TTL
    uart_id: optolink_uart_1
  - id: optolink_og
    name: "OG"
    port_type: TTL
    uart_id: optolink_uart_2
```

### Why the names in this file matter

The `esphome: name:` of the node and the `name:` of each serial proxy are what OptoV builds its
entity identities from. The proxy name also appears in the integration's title, which is how two
controllers are told apart there. That has one consequence worth knowing before you start.

**Your customisations survive a reinstall.** Enable a few extra entities by hand, put a device
in an area, rename something: remove OptoV and add it again, uploading your catalog again, and
Home Assistant restores all of it, because the identities do not change. They are yours, written in the file above, rather than
anything Home Assistant generated. If the ESP32 itself dies, flash the replacement with the same
configuration and the new hardware picks up exactly where the old one left off.

**The other side of the same coin:** renaming the node, or renaming a proxy, changes those
identities. Home Assistant then treats the entities as new ones, and manual enabling, areas and
custom names are lost. Pick names you are happy with before you build a dashboard on them.

## What you get

**Identification.** The integration reads the controller's full identification, device group,
device, hardware index, software index and one extra register where needed, and resolves the
exact variant. A device id alone is not enough: many controllers share one, and variants
behind the same id can differ by nearly a factor of two in what they expose.

**The base entity set.** What the controller itself puts on its own overview, operating and
trend menus is enabled out of the box. Everything else the catalog knows for your controller
exists as a disabled entity, so it costs nothing until you switch it on. Enable a single entity
from its settings page and it stays enabled; Home Assistant reloads the integration about 30
seconds later, and the entity is polled from then on.

**Tiers.** The options can switch on whole groups at once: diagnostics, commissioning, coding
and the expert layer. They are for a debugging session rather than everyday use. A heat pump
goes from about 80 datapoints to over 400 with everything on.

**Channel update rate and bus budget.** The Optolink interface communicates at 4800 baud,
where each request-response telegram takes roughly 65 ms. At the default 15-second poll interval,
the bus comfortably handles around 200 reads per cycle within a safe bus budget. With the base
entity set (typically 50–80 datapoints), all enabled entities are polled every cycle. When hundreds
of channels are enabled (such as with the expert tier), the scheduler prioritises fast-moving process
values and rotates the remaining entities across subsequent cycles according to urgency. Enabling
large numbers of channels therefore proportionately lowers the effective update frequency of individual
datapoints rather than overloading the bus. Live gateway sensors (`bus_load`, `datapoint_rate`,
`poll_duration`) let you monitor this directly.

**Asking the controller.** The catalog describes a whole family, so it lists datapoints your
unit does not have. The integration reads each one once and takes the controller's own answer:
an address it reports as not implemented is dropped, and a sensor whose health code says it is
not fitted is retired.

**Writing.** Setpoints, operating modes, party and eco mode, curve slope and level and the
weekly programmes are writable. Several settings can share one byte; the integration reads the
byte, changes its bits and writes it back, then reads it again to confirm.

**Losing the connection.** When the ESP32 restarts or drops off the network, the integration
reconnects by itself as soon as the node is back. When the controller stops answering, because
it is switched off, restarting, or the read head was taken off, every entity turns unavailable
instead of showing its last value as if it were current. Each poll cycle then makes a single
attempt, and the entities come back on their own once the controller answers again.

**Controller clock.** The controller has no time source of its own and drifts by minutes a
month, and every weekly programme runs against its clock. The integration corrects it when it
is more than a minute out, at most once an hour, and checks the controller's daylight-saving
settings against your time zone. Both can be switched off in the options.

**Fault history.** The controller's own fault buffer, decoded with the fault texts of your
exact controller family. The fault history is read on startup and refreshed in the background
every **15 minutes** (every 60 poll cycles at the default 15 s interval).

**Schedules.** Weekly programmes are read on startup and refreshed in the background every
**30 minutes** (every 120 poll cycles at the default 15 s interval). When a schedule is changed
from a dashboard card or service, the updated programme is read back immediately.

## Options

| Option | Default | What it does |
|---|---|---|
| Catalog language | German | Names of datapoints, circuits, enumeration values and fault texts. The languages offered are the ones your catalog was built with. |
| Poll interval | 15 s | How often a cycle starts. The base set is read every cycle; slower datapoints rotate through whatever bus time is left. |
| Diagnostics | off | Trending, statistics and the two diagnosis pages |
| Commissioning | off | Installer-level settings |
| Coding 2 | off | Expert coding parameters |
| Expert layer | off | Service-level parameters |
| Keep the clock in sync | on | Correct the controller clock from Home Assistant when it drifts |
| Verbose logging | off | Raise this integration's log level to debug without touching your `logger:` configuration |

Switching a tier on enables its entities; switching it off disables them again. **Entities you
switched on or off yourself are left alone.** Enable a single datapoint for a dashboard, or
disable one you do not want, and it stays that way through tier changes, restarts and
reinstalls. Everything you have not touched follows the tiers, also after removing and adding
the integration again.

The integration's own text, this options page, entity names it invents and the dashboard cards,
follows the Home Assistant language. Catalog text follows the catalog language above. The two
are independent.

## Dashboard cards

Both cards are served by the integration and registered as a dashboard resource, so they
appear in the card picker with no manual setup.

**OptoV Schedule** edits the weekly programmes. Without configuration it shows one tab per
programme the controller has; the visual editor lets you pick a subset, name the tabs, and show
a reading and a setpoint in the header. Editing is per day, with an *apply to* choice of the
day, the working week, the weekend or the whole week, preselected from the pattern the week is
already in.

```yaml
type: custom:optov-schedule-card
entities:                             # optional, otherwise every programme
  - sensor.<schedule sensor>
  - entity: sensor.<schedule sensor>
    name: Hot water                   # your own tab label
names:                                # optional, tab labels when showing every programme
  sensor.<schedule sensor>: Hot water
actual_entity: sensor.<any>           # optional, shown in the header
demand_entity: sensor.<any>           # optional
title: <text>                         # optional
```

**OptoV Fault History** lists the controller's fault buffer with date, text and code. The
editor offers exactly the codes your controller has logged, most frequent first, so routine
entries such as the controller noting its own restarts can be hidden.

```yaml
type: custom:optov-fault-history-card
hide_codes: [FF]                 # optional
max: 30                          # optional, entries to list
height: 420                      # optional, list height in pixels before it scrolls
```

**Names in your own cards.** Home Assistant puts the device name in front of an entity's name
when a card row has no name of its own, so a row reads "Warmwasser (6006) Max. WW-Temperatur".
To show just the entity's own name, which still follows the catalog language, set the row's name
to the entity part, or choose *Composed* with only *Entity* in the row editor:

```yaml
- entity: number.<entity>
  name:
    type: entity
```

## Services

| Service | Purpose |
|---|---|
| `optov.refresh_all` | Read every enabled datapoint on the next cycle, for example right after changing something at the controller's own panel |
| `optov.sync_clock` | Set the controller clock from Home Assistant now, whatever the drift |
| `optov.read_schedule` | Re-read one weekly programme |
| `optov.set_schedule_day` | Replace the switching windows of one or several days |
| `optov.set_schedule_window` | Change a single switching window |
| `optov.read_datapoint` / `write_datapoint` | Raw access to any address, for testing |

Programmes are addressed by the `schedule` attribute of their sensor. With more than one
controller set up, pass `config_entry_id` to say which one a call is for.

## Entity ids

Entity ids are built from the controller's model code, its circuit and the datapoint, not from
the device's display name. A reinstall therefore lands on exactly the same ids and keeps its
long-term statistics. Two identical controllers get a numeric suffix on the second.

## Troubleshooting

**Setup says the controller is not reachable.** Check that the ESPHome node is online and that
nothing else is connected to its serial proxy. The handshake needs the port to itself.

**All entities show "unavailable".** The controller does not answer, and the log says "The
controller does not answer". Check that it is switched on and that the read head sits on the
optical port. The entities come back by themselves once it answers again.

**Single entities show "unavailable".** The controller's own sensor-health code reports a fault
for that sensor, short circuit, open circuit or not fitted. The code is in the entity's
`sensor_status` attribute.

**A value looks wrong.** Switch on verbose logging in the options, note the entity's `address`
attribute, and open an issue with the log lines for that address. The catalog is a superset and
a wrong scaling or byte order for your variant is a catalog fix, not a code change.

**"Controller System ID ... is not supported in the database."** The catalog was built for a
different controller. Run VExtractor again and give it the id from that message, or answer `all`
to cover every controller at once.

**Setup asks which catalog to use.** There is more than one in `<config>/optov/`. Pick the one
that describes this controller, or upload another. The choice is stored with the entry, so each
controller can have its own.

**"Rebuild the catalog" or "update the integration" at startup.** The catalog and the
integration are versioned against each other and these two are out of step. Rebuild with the
current VExtractor and upload the new catalog with **Reconfigure** on the entry, or update the
integration, whichever the message asks for.

**The poll cycle is long.** The bus runs at 4800 baud and one datapoint is one round trip of
about 65 ms. Several hundred enabled datapoints take a couple of cycles per full pass; the
scheduler always reads the most overdue first, so nothing starves. The gateway device's
diagnostic sensors show the duty cycle and the datapoint rate.

## Contributing

Bug reports with the log lines around the problem and the entity's `address` attribute are the
most useful thing. Reports from controllers other than the one verified so far are especially
welcome, whether they work or not.

## How this was written

OptoV was written together with AI coding assistants, Claude, Gemini and GPT among them, with a
person directing the work, making the design decisions, and testing every part of it against a
real controller. Some lines were typed by a human and many were drafted by a model; treat it as
you would any other code, read it before you trust it, and report what is wrong.

## Licence

MIT, see [LICENSE](LICENSE). The licence covers this code. It does not cover the controller
catalog, which is not part of this repository and which each user builds for themselves.
