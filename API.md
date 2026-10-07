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
      "result": "complete",
      "cause": null,
      "print_time_s": 3038,
      "filament_m": 4.145, "filament_g": 12.4,
      "material": "PETG", "vendor": "Elegoo",
      "spools": [{"id": 2, "g": 12.4}],
      "pauses": [],
      "reference": "042",
      "has_picture": true,
      "picture_url": "/api/external/v1/history/3c47d263…/picture"
    }
  ]
}
```

Notes: `result` is `complete`, `cancelled` (stopped) or `error`. For the last two `cause` has
`message`, `category`, `code` and `initiated_by` where the printer reported them. `material`,
`vendor`, `started_at`, `spools` and `pauses` are missing on older prints. `reference` is `""` when none is set.

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

### `GET /`

Name, version and the scope of the key you used.

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
