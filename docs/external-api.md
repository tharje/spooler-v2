# Spooler external API (v1)

A small HTTP API that lets another program (a CRM, a dashboard, a script) read Spooler's
**print history**, the **picture** saved with each print, the **statistics** and the list of
**printers**, and set the **reference number** of a print.

It cannot control printers. There is no endpoint that starts, pauses, stops, heats, moves or
uploads anything, and there never will be one in `v1` (see [Versioning](#versioning-and-compatibility)).

- Base address: `http://<your-spooler>:8080/api/external/v1`
- Off until you switch it on (Settings → **API access**); then reachable only with an **API key**.
- Responses are JSON, except the picture endpoint (a JPEG). Errors are JSON too: `{"error": "…"}`.
- Dates and times are **local time without a time zone**, `YYYY-MM-DDTHH:MM:SS`, exactly as Spooler stores them.
- Everything below is a contract: see [Versioning and compatibility](#versioning-and-compatibility).

## Authentication

Every request needs a key:

```
Authorization: Bearer spl_xxxxxxxxxxxxxxxx
```

**Creating a key.** Settings → **API access** → switch on **Allow API access**, then under
**Create a key** give it a name (the program that will use it) and a scope:

| Scope | Can |
|---|---|
| `read` ("Read only") | everything except changing anything |
| `write` ("Read + change reference numbers") | everything `read` can, plus `PATCH /history/{id}` for the reference number |

Copy the key (it starts with `spl_`). It is shown **once**; Spooler stores only a hash of it. Lost it? Revoke it
and create a new one. **Revoking** (Settings → API access → Revoke) takes effect immediately. At most 20 keys.

A key opens this API and nothing else in Spooler (not the web interface, not the printers). Give every program its own key.

**Lock-out.** Ten wrong keys within five minutes from one address block that address for a while (HTTP `429`).

## Conventions

- `id` of a print is a 32-character hex string that **never changes**. Store it in your own system.
- Numbers: `print_time_s` seconds, `filament_m` metres, `filament_mm` millimetres, `filament_g` grams.
- Fields that Spooler doesn't know (older prints, no Spoolman) are `null` (or an empty list / `""` for `reference`).
- `limit`/`offset` paging; `total` is the number of matching prints before paging.

## Endpoints

### `GET /`

Name, version of the API and the scope of the key you used.

```json
{"name": "Spooler external API", "version": 1, "scope": "read",
 "endpoints": ["GET /history", "GET /history/{id}", "GET /history/{id}/picture", "PATCH /history/{id}", "GET /stats", "GET /printers"]}
```

### `GET /history`

Prints, newest first.

| Query | Meaning |
|---|---|
| `from`, `to` | `YYYY-MM-DD`; both inclusive, the whole day counts |
| `printer` | printer id (`printer_id` in the results) |
| `q` | text found in the file name or the reference (case-insensitive) |
| `reference` | exact reference number |
| `result` | `complete`, `cancelled` or `error` |
| `ended_after` | only prints that ended strictly later than this date or `YYYY-MM-DDTHH:MM:SS` (for syncing, below) |
| `order` | `desc` (newest first, default) or `asc` (oldest first) |
| `include` | `raw` adds a `raw` object to every item: the stored record exactly as Spooler has it |
| `limit`, `offset` | paging; `limit` defaults to 50, at most 500 |

```
curl -H "Authorization: Bearer $KEY" \
  "http://spooler.local:8080/api/external/v1/history?from=2026-10-01&result=complete&limit=20"
```

```json
{
  "total": 42, "limit": 20, "offset": 0,
  "items": [
    {
      "id": "3c47d263bb854cd8b5cec6d9e2f92207",
      "ended_at": "2026-10-05T21:04:49",
      "started_at": "2026-10-05T20:14:00",
      "printer_id": "42b27b5a…", "printer_name": "CC2",
      "file": "benchy.gcode",
      "result": "complete", "result_label": "Finished",
      "cause": null,
      "print_time_s": 3038,
      "filament_mm": 4144.5, "filament_m": 4.145, "filament_g": 12.4,
      "material": "PETG", "vendor": "Elegoo",
      "spools": [{"id": 2, "g": 12.4, "name": "Elegoo PETG Pro", "material": "PETG", "vendor": "Elegoo", "color_hex": "FF0000"}],
      "pauses": [{"since": "2026-10-05T20:40:00", "until": "2026-10-05T20:43:10", "duration_s": 190,
                  "category": "filament_runout", "category_label": "Filament runout",
                  "initiated_by": "printer", "initiated_by_label": "The printer itself"}],
      "reference": "042",
      "has_picture": true,
      "picture_url": "/api/external/v1/history/3c47d263bb854cd8b5cec6d9e2f92207/picture"
    }
  ]
}
```

Fields of an item:

| Field | Type | Meaning |
|---|---|---|
| `id` | string | permanent id of the print |
| `ended_at` / `started_at` | string / string or null | when it ended / started (`started_at` is `null` if unknown) |
| `printer_id`, `printer_name` | string | the printer |
| `file` | string | the file that was printed |
| `result` | string | `complete`, `cancelled` (stopped) or `error` |
| `result_label` | string | `Finished`, `Stopped` or `Failed` |
| `cause` | object or null | `null` for finished prints; otherwise `text` (readable), `message`, `category`, `category_label`, `code`, `initiated_by`, `initiated_by_label` (each may be `null`) |
| `print_time_s` | number | print time in seconds |
| `filament_mm`, `filament_m`, `filament_g` | number | filament used |
| `material`, `vendor` | string or null | from Spoolman at the time of the print |
| `spools` | list | per spool used: `id` (Spoolman id), `g`, and `name`, `material`, `vendor`, `color_hex` looked up in Spoolman when the request is made (`null` if Spoolman can't be reached or the spool is gone) |
| `pauses` | list | per pause: `since`, `until`, `duration_s`, `category`, `category_label`, `initiated_by`, `initiated_by_label` |
| `reference` | string | the reference number; `""` when none |
| `has_picture`, `picture_url` | bool, string or null | whether a picture exists, and where to fetch it |
| `raw` | object | only with `include=raw` |

### `GET /history/{id}`

One print, same shape as an item above (also accepts `include=raw`). `404` if there is no such print.

### `GET /history/{id}/picture`

The JPEG taken when the print ended. `404` if the print has no picture (`has_picture` is `false`).

```
curl -H "Authorization: Bearer $KEY" -o print.jpg \
  http://spooler.local:8080/api/external/v1/history/3c47d263bb854cd8b5cec6d9e2f92207/picture
```

### `PATCH /history/{id}`

Set the **reference number** of a print. Needs a `write` key (a `read` key gets `403`).

```
curl -X PATCH -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"reference": "042"}' \
  http://spooler.local:8080/api/external/v1/history/3c47d263bb854cd8b5cec6d9e2f92207
```

```json
{"ok": true, "reference": "042"}
```

`reference` is text or a number, at most 40 characters, control characters removed, surrounding spaces trimmed;
an empty string clears it. Nothing else can be changed (any other field gives `400`); prints and pictures cannot be
deleted. Request bodies over 16 KB give `413`. Browsers showing the print list update by themselves.

### `GET /stats`

The numbers behind Spooler's Stats page. Query: `from`, `to`, `printer` (as above).

```json
{
  "prints": 42, "results": {"complete": 36, "cancelled": 4, "error": 2}, "success_rate": 85.7,
  "hours": 113.78, "grams": 2818.0, "avg_print_s": 4819,
  "by_material": {"PETG": 1200.5}, "by_printer": {"CC2": {"prints": 30, "hours": 85.9, "grams": 1272.5}},
  "failure_reasons": {"Hotend isn't heating (103)": 2},
  "bucket": "week", "series": [{"key": "2026-10-05", "prints": 4, "grams": 40.1, "hours": 6.2}],
  "from": "2026-04-10", "to": "2026-10-05"
}
```

`success_rate` and `avg_print_s` are `null` when there is nothing to average. `bucket` is `day`, `week` or `month`,
chosen from the length of the period; `series` has one entry per bucket including empty ones.

### `GET /printers`

Your printers: `id`, `name`, `type` (`cc1`, `cc2`, `prusa`, `moonraker`), `connected` and `state`. No addresses or
access codes. The `id` matches `printer_id` on the prints.

## Reference numbers

A reference number is a short label you put on a print, for example an order or customer number. You can type it on the
print's page under **Stats → Prints**, or set it through this API. It is shown in Spooler, searchable (`q`, `reference`)
and included in the CSV export. It does not have to be unique.

## Linking prints to customers (a CRM)

Spooler does not know about customers; keep that link in your own system.

- **Key:** store a print's `id` against the customer or order.
- **Reference number:** put your order or customer number on the print and find prints again with `GET /history?reference=…`.
- **Syncing new prints:** remember the `ended_at` of the last print you imported, then ask for
  `GET /history?ended_after=<that>&order=asc&limit=500` and import what comes back; repeat while `total` exceeds what you
  got. Poll every few minutes. Spooler does not call out to other programs on its own (its *Webhook* notification channel
  can post an event to an address you choose when a print ends).
- **Pictures:** fetch `picture_url` when you need the image.

## Errors

JSON `{"error": "…"}` with one of these statuses:

| Status | Meaning |
|---|---|
| `400` | bad request: unknown or invalid parameter, bad JSON, a field that can't be changed, an invalid reference |
| `401` | missing or wrong API key (response has `WWW-Authenticate: Bearer`) |
| `403` | API access is switched off (`{"error": "feature_disabled", "feature": "external_api"}`), or the key is read-only |
| `404` | no such print, picture or endpoint |
| `413` | request body too large (over 16 KB) |
| `429` | too many wrong keys from your address; try again later |

## Versioning and compatibility

The path carries the version: `/api/external/v1`. Within `v1`:

- **Fields can be added, never removed, renamed or changed in type or meaning.** Write your client to ignore fields it doesn't know.
- **Endpoints and query parameters are not removed.** New ones may appear.
- **Values of enumerations** (`result`, `bucket`, `scope`) can gain new members only in a way an old client can safely treat as "unknown".
- The API **never controls printers** (no start, pause, stop, temperature, fans, axes, upload), with any key or scope.
- Breaking changes need a new version, `/api/external/v2`. `v1` then keeps working for at least one more major
  release of Spooler, announced in the release notes of the version that introduces `v2`.

Contract tests in the repository (`tests/test_external_api_contract.py`) fail if a field in this document disappears or changes type.

## Security notes

- Keep keys secret; revoke any that may have leaked.
- Anyone who can reach Spooler's address can try keys. A key is long and random, wrong keys are rate limited, and the API is
  off by default, but if Spooler is reachable from the internet (for example through a tunnel), use HTTPS and prefer `read` keys.
- Keys are stored hashed in `api_tokens.json` (not in git) and are part of backups, so a restored backup keeps working keys.
- Cross-origin requests are allowed (`Access-Control-Allow-Origin: *`) so a web page can call the API. This is safe because the
  API never uses cookies; it needs the key you send.
