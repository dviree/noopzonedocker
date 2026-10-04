# NoopZone Docker

Self-hosted receiver + health dashboard for **NoopZone / NOOP Push Protocol 1.0**.

It is designed for a NAS: the iPhone remains the authoritative collector, while this container keeps a one-way historical copy of your WHOOP/NoopZone **and Apple Health** data. No WHOOP cloud account is required for this path.

## What is implemented

- Authenticated `GET /api/noop` capability negotiation.
- Authenticated `POST /api/noop` NDJSON batches.
- gzip and identity request bodies.
- All 12 core NOOP Push 1.0 streams.
- Optional Apple Health extension streams: `appleDaily`, `metricSeries`, and `appleStepHour`.
- Direct Apple Health import from the standard `export.zip` or `export.xml`; this does **not** require changes to the NoopZone app.
- Idempotent batch receipts: a retry does not duplicate data.
- Append streams and multipart `replace_window` streams.
- Persistent SQLite/WAL database under `./data`.
- Dashboard for recovery, sleep, HRV, resting HR, strain, sleep sessions, workouts, plus Apple Health steps, calories, VO₂max, weight, body-fat/BMI, and stored-row counts.
- Readiness endpoint: `GET /healthz`.

## NAS install

```bash
git clone https://github.com/dviree/noopzonedocker.git
cd noopzonedocker
cp .env.example .env
```

Edit `.env` and set a long random `NOOP_PUSH_TOKEN`. Then:

```bash
docker compose up -d --build
```

Open:

```text
http://NAS-IP:8700
```

The NOOP Push endpoint is:

```text
http://NAS-IP:8700/api/noop
```

### Apple Health

In Apple Health, export your health data and upload the resulting `export.zip` from the **Import Apple Health** card in the dashboard. Manual HealthKit import does not require a token, but it is restricted to LAN/private/Tailscale clients.

The importer streams `export.xml` and stores daily HR/HRV/RHR/SpO₂/respiration/activity/body-composition metrics, sleep stages and workouts. Re-importing a newer Apple export replaces only the previous `healthkit-export` snapshot; WHOOP/NOOP rows are left untouched.

For access away from home, prefer HTTPS or Tailscale rather than exposing port 8700 directly to the internet.

## Backups

Back up the `data/` directory. The receiver state ID and all health records live in `data/noopzone.sqlite3`.

## Security

`NOOP_PUSH_TOKEN` authenticates NOOP Push writes from client apps. Manual Apple Health import is restricted to LAN/private/Tailscale clients instead. `DASHBOARD_TOKEN` is optional and protects JSON dashboard APIs when set. Keep secrets out of git.

## Protocol

The receiver follows NOOP's documented Push Protocol 1.0 contract for the core registry. Three optional Apple Health extension streams are advertised separately so a strict upstream NOOP client can ignore them. Apple Health can also be imported directly from Apple's export archive, independently of the NoopZone app. The server never sends health records or commands back to the phone.

## Attribution

The dashboard direction is inspired by the MIT-licensed [Vitals](https://github.com/DocStream-Oficial/vitals) self-hosted health project. This repository uses an independent minimal implementation focused on NOOP Push rather than Vitals' OAuth/cloud-source stack.

NOOP/NoopZone is independent and is not affiliated with WHOOP, Inc.
