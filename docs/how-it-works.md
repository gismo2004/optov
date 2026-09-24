# How it works

What the integration does with a controller once it is set up, and how often it does it.

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
datapoints rather than overloading the bus. The first cycle after Home Assistant starts or the
integration reloads is the exception: it reads every enabled channel in one go, however long
that takes, and values appear as they are read. Live gateway sensors (`bus_load`, `datapoint_rate`,
`poll_duration`) let you monitor this directly.

**Asking the controller.** The catalog describes a whole family, so it lists datapoints your
unit does not have. The integration reads each one once and takes the controller's own answer:
an address it reports as not implemented is dropped, and a sensor whose health code says it is
not fitted is retired.

**Writing.** Setpoints, operating modes, party and eco mode, curve slope and level and the
weekly programmes are writable. Several settings can share one byte; the integration reads the
byte, changes its bits and writes it back, then reads it again to confirm.

**Losing the connection.** When the ESPHome node restarts or drops off the network, the integration
reconnects by itself as soon as the node is back. When the controller stops answering, because
it is switched off, restarting, or the read head was taken off, every entity turns unavailable
instead of showing its last value as if it were current. Each poll cycle then makes a single
attempt, and the entities come back on their own once the controller answers again.

**Controller clock.** The controller has no time source of its own and drifts by minutes a
month, and every weekly programme runs against its clock. The integration corrects it when it
is more than a minute out, at most once an hour, and checks the controller's daylight-saving
settings against your time zone. Both can be switched off in the options. The clock's own
entity shows the **drift in seconds** rather than the time: the time would be a new state
every poll, and the drift is what you would look at. The controller's time is now plus drift.

**Fault history.** The controller's own fault buffer, decoded with the fault texts of your
exact controller family. Boilers with a burner automat get a second sensor for the automat's
own fault records, with the texts of the automat that is actually fitted; the fault-history card
lists both. The fault history is read on startup and refreshed in the background
every **15 minutes** (every 60 poll cycles at the default 15 s interval).

**Schedules.** Weekly programmes are read on startup and refreshed in the background every
**30 minutes** (every 120 poll cycles at the default 15 s interval). When a schedule is changed
from a dashboard card or service, the updated programme is read back immediately.
