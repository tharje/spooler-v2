# Elegoo Centauri Carbon 2 (CC2) — Integration Notes

Reference notes for adding CC2 support to Spooler. CC2 uses a different
transport than CC1: instead of SDCP over WebSocket, the printer **runs its own
MQTT broker** that clients connect to directly over the LAN.

> **Status legend**
> - ✅ verified against community implementations (LegalMarc/centauri-sentinel, danielcherubini/elegoo-homeassistant)
> - ⚠️ likely correct but should be confirmed against your own printer + firmware before relying on it
>
> Always test against real hardware. Firmware updates can change topics, ports, and payload shapes.

---

## 1. Architecture: CC1 vs CC2

| | CC1 | CC2 |
|---|---|---|
| Transport | SDCP v3.0 over WebSocket | MQTT (printer hosts its own broker) ✅ |
| Camera | MJPEG | MJPEG ✅ |
| Web UI on printer | Yes | No (Orca/CC1-style connection fails) ✅ |
| Discovery | UDP broadcast | UDP broadcast ⚠️ (near-identical) |

The key mental model: on CC2 the **printer is the broker**. Your client connects
*to* the printer, subscribes to its status topic, and publishes commands back.

---

## 2. Connection

```
Host:     <printer LAN IP>
Port:     1883                          ✅
Username: elegoo                        ✅
Password: <access code>                 ✅  (Settings → Network on the printer; the 5-digit code)
```

`aiomqtt` is a good fit for Spooler's async FastAPI stack:

```python
import aiomqtt

async with aiomqtt.Client(
    hostname=printer_ip,
    port=1883,
    username="elegoo",
    password=access_code,
) as client:
    await client.subscribe("elegoo/+/api_status")
    async for message in client.messages:
        handle(message)
```

---

## 3. Topics

| Direction | Topic | Notes |
|---|---|---|
| Subscribe (status) | `elegoo/<serial>/api_status` | ✅ Use wildcard `elegoo/+/api_status` if you don't know the serial yet |
| Publish (command) | `elegoo/<serial>/<client_id>/api_request` | ✅ `client_id` is a UUID you generate |

**Serial discovery trick:** subscribe with the `+` wildcard, then read the serial
out of the topic string on the first status message:

```python
parts = str(message.topic).split("/")   # ["elegoo", "<serial>", "api_status"]
serial = parts[1]
```

**Important ordering rule:** ⚠️ Do not publish commands before you've received at
least one status message. The printer only listens on its own serial-based topic,
so a command sent to a guessed topic is a silent no-op — no error, no effect. This
is a likely cause of "MQTT connects but nothing happens".

---

## 4. Status payload (method 6000)

The printer pushes a status message roughly once per second. Relevant fields:

```
result.print_status.state              → printing / paused / idle      ✅
result.print_status.print_duration     → seconds elapsed                ✅
result.print_status.remaining_time_sec → seconds remaining              ⚠️
result.print_status.current_layer      → current layer                  ⚠️
result.print_status.filename           → file currently printing        ✅
result.machine_status.progress         → percent complete               ⚠️
result.extruder.temperature / .target  → nozzle temp / target           ✅
result.heater_bed.temperature / .target→ bed temp / target              ✅
result.external_device.camera          → camera present flag            ⚠️
result.file_list[].layer               → total layers (look up by filename) ⚠️
```

**Partial-update quirk:** ✅ status messages are sometimes *partial* — a frame may
contain only `gcode_move` and `print_status`, not the full object. Do **not**
replace your accumulated state with each message; **deep-merge** into it instead,
or live values will flicker to null. Guard the merge against unbounded key growth.

```python
def deep_merge(base: dict, incoming: dict, max_keys: int = 500) -> dict:
    for k, v in incoming.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v, max_keys)
        else:
            base[k] = v
    return base
```

---

## 5. Commands

Publish to `elegoo/<serial>/<client_id>/api_request`:

```python
{"id": <random_int>, "method": 1001}   # pause   ✅
{"id": <random_int>, "method": 1002}   # resume  ✅
{"id": <random_int>, "method": 1003}   # stop    ✅
```

- ⚠️ **Light toggle:** not verified for CC2 in the community code reviewed. Find it
  by sniffing real traffic (see §8) rather than guessing method codes.
- **Debounce:** advance your debounce timestamp only on a *successful* publish, so a
  double-click doesn't fire two commands.

---

## 6. Camera

```
http://<printer IP>:8080/mjpeg          ✅  (no auth)
```

Simpler than CC1 — there's no separate authenticated port. The same MJPEG
single-frame grabber works for both printers, which means failure-detection /
snapshot features are printer-agnostic at the image layer.

---

## 7. Discovery & system info

```
UDP discovery:  port 3000, payload {"id": 0, "method": 7000}        ⚠️
System info:    GET http://<ip>/system/info?X-Token=<access code>   ⚠️
```

CC2 discovery is close enough to CC1 that the existing UDP discovery code likely
needs only a variant, not a rewrite.

---

## 8. Finding undocumented commands (e.g. light)

For anything not verified above, observe real traffic instead of guessing:

1. Subscribe to **everything** from the printer: `elegoo/#`, log every message.
2. Trigger the action from the official Elegoo app / printer screen.
3. Watch which `method` code appears — that's your answer.
4. Send the same command yourself and confirm the printer reacts.

⚠️ Don't brute-force method codes in a loop, especially mid-print. Find them by
observation.

---

## 9. Suggested Spooler integration shape

CC2 slots into the existing adapter pattern alongside the CC1/SDCP adapter:

```
CC2MqttAdapter
├── connect()        → aiomqtt client, username=elegoo, password=access_code
├── subscribe()      → elegoo/+/api_status
├── on_message()     → deep-merge into accumulated state; capture serial from topic
├── pause/resume/stop→ publish method 1001/1002/1003 to api_request topic
├── camera_url()     → http://<ip>:8080/mjpeg
└── discover()       → UDP :3000, method 7000
```

Keep the normalized status model identical between CC1 and CC2 so the GUI,
filament tracking, and history logging don't care which printer type they're
talking to.

---

## Credits — ⚠️ ACTION REQUIRED BEFORE RELEASE

**Before publishing Spooler, credit the GitHub authors whose work made CC2 support
possible.** None of the CC2 protocol details here were reverse-engineered from
scratch — they come from reading these open-source projects. Crediting them is both
correct open-source etiquette and the right thing to do.

Add a "Credits" / "Acknowledgements" section to the main Spooler README linking to:

- **[LegalMarc/centauri-sentinel](https://github.com/LegalMarc/centauri-sentinel)** —
  primary CC2 reference. MQTT client (`username=elegoo` + access-code password),
  topic structure, partial-status deep-merge, MJPEG single-frame grabber, and the
  `verified-assumptions.md` documentation approach.
- **[danielcherubini/elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant)** —
  `cc2_mqtt` transport type and the access-code configuration field.
- **[WalkerFrederick/sdcp-centauri-carbon](https://github.com/WalkerFrederick/sdcp-centauri-carbon)**
  and **[OpenCentauri/cc-fw-tools](https://github.com/OpenCentauri)** — SDCP and MQTT
  protocol documentation.

Suggested wording for the Spooler README:

> ### Acknowledgements
> CC2 (Elegoo Centauri Carbon 2) support would not have been possible without the
> reverse-engineering work of the following projects. Huge thanks to their authors:
> - [centauri-sentinel](https://github.com/LegalMarc/centauri-sentinel) by LegalMarc
> - [elegoo-homeassistant](https://github.com/danielcherubini/elegoo-homeassistant) by danielcherubini

Also check each project's LICENSE — if you reuse any code directly (not just the
protocol knowledge), you may need to retain their license notice.

---

Verify all ⚠️ items against your own printer and firmware version, and keep a
`verified-assumptions.md` log noting what you confirmed, when, and on which firmware.
