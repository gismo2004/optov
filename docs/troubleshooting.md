# Troubleshooting

**Setup says the controller is not reachable.** Check that the ESPHome node is online and that
nothing else is connected to its serial proxy. The handshake needs the port to itself.

**Setup fails with "No handler registered for URI scheme esphome-hass".** Home Assistant offers
the serial proxies of ESPHome nodes once its `usb` integration has been loaded, which happens
with `default_config`. Restart Home Assistant once after installing OptoV and it is there.

**All entities show "unavailable".** The controller does not answer, and the log says "The
controller does not answer". Check that it is switched on and that the read head sits on the
optical port. The entities come back by themselves once it answers again.

**Reporting a problem.** The integration entry has a *Download diagnostics* item in its menu.
It holds the controller, its catalog version, the generated entity set, which datapoints turned
out not to be fitted and how the bus is doing, with the node's address and key left out.

**Single entities show "unavailable".** The controller's own sensor-health code reports a fault
for that sensor, short circuit, open circuit or not fitted. The code is in the entity's
`sensor_status` attribute.

**A value looks wrong.** Switch on *Enable debug logging* in the integration's menu, note the entity's `address`
attribute, and open an issue with the log lines for that address. The catalog is a superset and
a wrong scaling or byte order for your variant is a catalog fix, not a code change.

**"The controller reports System ID ... The catalog has no entry for that."** The message
names the System ID, the hardware index and the software index your controller reports, and the
variants the catalog holds for that System ID. Usually the catalog was built for a different
controller: run VExtractor again and give it the id from that message, or answer `all` to cover
every controller at once. If the System ID is in the catalog but no variant fits, the three
values belong in an issue -- the controller is describing itself in a way the service software
never recorded.

**Setup asks which catalog to use.** There is more than one in `<config>/optov/`. Pick the one
that describes this controller, or upload another. The choice is stored with the entry, so each
controller can have its own.

**"Rebuild the catalog" or "update the integration" at startup.** The catalog and the
integration are versioned against each other and these two are out of step. Rebuild with the
current VExtractor and upload the new catalog with **Reconfigure** on the entry, or update the
integration, whichever the message asks for. A repair *Catalog built for an older structure*
is the mild form of the same thing: the entry runs, but a rebuild would add what the catalog
has gained since, and the repair goes away by itself once it does.

**The poll cycle is long.** The bus runs at 4800 baud and one datapoint is one round trip of
about 65 ms. Several hundred enabled datapoints take a couple of cycles per full pass; the
scheduler always reads the most overdue first, so nothing starves. The gateway device's
diagnostic sensors show the duty cycle and the datapoint rate.

## Removing the integration

Delete the entry under **Settings → Devices & services → OptoV**. Its entities, devices and the dashboard resource go with it, and so does its catalog file in `<config>/optov/`, unless another entry uses the same file -- keep your own copy of the catalog. Uninstalling through HACS afterwards removes the code; restart Home Assistant once more.
