# Services

| Service | Purpose |
|---|---|
| `optov.refresh_all` | Read every enabled datapoint on the next cycle, for example right after changing something at the controller's own panel |
| `optov.sync_clock` | Set the controller clock from Home Assistant now, whatever the drift |
| `optov.read_schedule` | Re-read one weekly programme |
| `optov.set_schedule_day` | Replace the switching windows of one or several days |
| `optov.set_schedule_window` | Change a single switching window |
| `optov.read_datapoint` / `write_datapoint` | Raw access to any address, for testing |
| `optov.clear_orphaned_statistics` | Delete the long-term statistics of entities the integration switched off; `include_user_disabled: true` also takes the ones you disabled yourself. Returns the ids deleted. |

Programmes are addressed by the `schedule` attribute of their sensor. With more than one
controller set up, pass `config_entry_id` to say which one a call is for.
