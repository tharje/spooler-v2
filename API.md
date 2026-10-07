# Spooler external API

A small, read-mostly HTTP API that lets another program read your **print history**,
the **picture** saved with each print and the **statistics**, and change the
**reference number** of a print.

It is off until you switch it on (Settings → API access), and it is reachable only
with an **API key**. A key opens this API and nothing else in Spooler.

- Base address: `http://<your-spooler>:8080/api/external/v1`
- Responses are JSON (the picture endpoint returns a JPEG).
- Dates and times are local time as `YYYY-MM-DDTHH:MM:SS` (no time zone), as Spooler stores them.

## Getting a key

1. Settings → **API access**, switch **Allow API access** on.
2. Under **Create a key**, give it a name (the program that will use it) and choose
   - **Read only**, or
   - **Read + change reference numbers**.
3. Copy the key (it starts with `spl_`). It is shown **once**; Spooler keeps only a hash.
   Lost it? Revoke it and create a new one. Revoking takes effect immediately.

Send the key on every request:

```
Authorization: Bearer spl_xxxxxxxxxxxxxxxx
```

Ten wrong keys within five minutes from the same address lock that address out for a while (HTTP 429).

## Endpoints

### `GET /history`

Prints, newest first.

| Query | Meaning |
|---|---|
| `from`, `to` | `YYYY-MM-DD`; both inclusive, the whole day counts |
| `printer` | printer id (`printer_id` in the results) |
| `q` | text found in the file name or the reference (case-insensitive) |
| `reference` | exact reference number |
| `result` | `complete`, `cancelled` or `error` |
| `ended_after` | only prints that ended strictly later than this date or `YYYY-MM-DDTHH:MM:SS` ("what is new since my last sync") |
| `order` | `desc` (newest first, default) or `asc` (oldest first) |
| `include` | `raw` adds a `raw` object to every item: the stored record exactly as Spooler has it, so nothing is lost if Spooler stores more later |
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
      "picture_url": "/api/external/v1/history/3c47d263…/picture"
    }
  ]
}
```

Notes:

- `result` is `complete`, `cancelled` (stopped) or `error`; `result_label` is the same in plain words.
- For the last two, `cause` has the raw `message`, `category`, `code` and `initiated_by` where the printer reported
  them, readable versions (`category_label`, `initiated_by_label`) and a ready-made `text` such as
  `Hotend isn't heating (code 103)`.
- `spools` carry the Spoolman spool id and the grams used from it, plus the spool's name, material, vendor and
  colour looked up in Spoolman at request time (`null` when Spoolman can't be reached or the spool is gone).
- `material`, `vendor`, `started_at`, `spools` and `pauses` are missing on older prints (`null` / empty).
  `reference` is `""` when none is set.
- Add `?include=raw` when you want literally everything Spooler stored about a print.

### `GET /history/{id}`

One print, same shape as an item above. `404` if there is no such print.

### `GET /history/{id}/picture`

The JPEG taken when the print ended. `404` if that print has no picture (`has_picture` is `false`).

```
curl -H "Authorization: Bearer $KEY" -o print.jpg \
  http://spooler.local:8080/api/external/v1/history/3c47d263bb854cd8b5cec6d9e2f92207/picture
```

### `PATCH /history/{id}`

Change the reference number. Needs a **Read + change reference numbers** key (a read-only key gets `403`).

```
curl -X PATCH -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"reference": "042"}' \
  http://spooler.local:8080/api/external/v1/history/3c47d263bb854cd8b5cec6d9e2f92207
```

`reference` is text or a number, at most 40 characters; an empty string clears it.
Nothing else can be changed through the API (any other field gives `400`), and prints and pictures cannot be deleted.
Open browsers showing the print list update by themselves.

### `GET /stats`

The same numbers as the Stats page. Query: `from`, `to`, `printer` (as above). Returns totals,
results, hours, grams, filament by material and by printer, why prints stopped, and a per-day/week/month series.

### `GET /printers`

Your printers: `id`, `name`, `type` (`cc1`, `cc2`, `prusa`, `moonraker`), `connected` and `state`.
No addresses or access codes. The `id` matches `printer_id` on the prints.

### `GET /`

Name, version and the scope of the key you used.

## Linking prints to customers (a CRM)

Spooler does not know about customers; keep that link in your own system.

- **Key:** a print's `id` never changes, so store `id` against the customer or order in your CRM.
- **Reference number:** put your order or customer number on the print (`PATCH`, or type it on the print's page in
  Spooler) and look prints up again with `GET /history?reference=…`. It shows in Spooler and in the CSV export too.
- **Syncing new prints:** remember the `ended_at` of the last print you imported, then ask for
  `GET /history?ended_after=<that>&order=asc&limit=500` and import what comes back. Poll every few minutes; Spooler
  does not call out to other programs. (If you want a push when a print ends, Spooler's webhook notification channel
  can post to an address of yours.)
- **Pictures:** fetch `picture_url` when you need the image; it is a plain JPEG.

## Errors

JSON `{"error": "…"}` with the status: `400` bad request, `401` missing or wrong key, `403` API access is
off or the key is read-only, `404` not found, `429` too many failed attempts.

## Security notes

- Keep keys secret and give each program its own, so you can revoke one without touching the others.
- Anyone who can reach Spooler's address can try keys. A key is long and random (not guessable), and failures
  are rate limited, but if Spooler is reachable from the internet (for example through a tunnel), use HTTPS
  and prefer read-only keys.
- Keys are stored hashed in `api_tokens.json` (not in git). They are part of backups, so a restored backup keeps working keys.
- The API allows cross-origin requests (`Access-Control-Allow-Origin: *`) so a web page can call it; this is safe
  because it never uses cookies, only the key you send.
