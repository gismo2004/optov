# ESPHome configuration

A minimal node. The example runs on an ESP32; change the board block and the pins to your own
hardware and adapter. The line settings below are what the
Optolink port expects; OptoV sets the same values on the proxy itself whenever it opens the
port, so they are what the node uses on its own rather than something you have to get right.

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

Do not wire the adapter to the board's serial console, the `TX`/`RX` pins used for flashing and
logging. Boards print their boot messages there on every restart, before ESPHome takes over, and
with the adapter on those pins they go straight into the controller's optical port. Use two other
pins your board can run a UART on.

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

## Why the names in this file matter

The `esphome: name:` of the node and the `name:` of each serial proxy are what OptoV builds its
entity identities from. The proxy name also appears in the integration's title, which is how two
controllers are told apart there. That has one consequence worth knowing before you start.

**Your customisations survive a reinstall.** Enable a few extra entities by hand, put a device
in an area, rename something: remove OptoV and add it again, uploading your catalog again, and
Home Assistant restores all of it, because the identities do not change. They are yours, written in the file above, rather than
anything Home Assistant generated. If the node itself dies, flash the replacement with the same
configuration and the new hardware picks up exactly where the old one left off.

**The other side of the same coin:** renaming the node, or renaming a proxy, changes those
identities. Home Assistant then treats the entities as new ones, and manual enabling, areas and
custom names are lost. Pick names you are happy with before you build a dashboard on them.
