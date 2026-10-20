# Spaghetti detection

Spooler can watch a running print through the printer's camera and send a notification, with the picture, when the print looks like it is failing (spaghetti). The detection itself is done by the open source [Obico](https://github.com/TheSpaghettiDetective/obico-server) ML API (AGPL-3.0), which you run yourself as a separate container. Spooler only sends it pictures and reads the answer; nothing is ever done to the printer.

## 1. Run the Obico ML API

Obico's ML API is built from the Obico source (about 1 GB of download). On the machine that runs Spooler, or any machine Spooler can reach:

```bash
git clone https://github.com/TheSpaghettiDetective/obico-server.git
cd obico-server
cat > docker-compose.ml.yml <<'YAML'
services:
  ml_api:
    ports:
      - "3333:3333"
YAML
docker compose -f docker-compose.yml -f docker-compose.ml.yml up -d ml_api
```

Only the `ml_api` service is started, not the whole Obico server. To require an access token, set `ML_API_TOKEN` under `ml_api` → `environment` in the compose file first.

## 2. Set it up in Spooler

1. **Settings → Features → Spaghetti detection**: switch on.
2. **Settings → Services and data → Spaghetti detection (Obico)**:
   - **Detector address**: for example `http://192.0.2.10:3333`.
   - **Detector access token**: only if you set `ML_API_TOKEN`.
   - **Spooler's address as the detector sees it**: for example `http://192.0.2.10:8080`. The ML API fetches each picture from Spooler itself, so this must be an address the detector container can reach (not `localhost` unless both run in the same network namespace).
   - Press **Test connection**.
3. **Settings → Notifications → Possible print failure**: switch on (and *Include photo* if you want the picture).

## How it decides

While a printer is printing, Spooler takes a picture every *Seconds between pictures* (default 60). A picture counts as suspect when the detector's confidence is at least *Sensitivity* (default 50 %). When *Pictures in a row* (default 3) are suspect, one notification is sent for that print. Lower sensitivity or fewer pictures in a row warns earlier but gives more false alarms.

Each picture is handed to the detector on a one-time web address that works once, for 30 seconds, and is never stored.
