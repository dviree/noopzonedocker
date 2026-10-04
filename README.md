# NoopZone Docker

Self-hosted receiver + health dashboard for **NoopZone / NOOP Push Protocol 1.0**.

It is designed for a NAS: the iPhone remains the authoritative collector, while this container keeps a one-way historical copy of your WHOOP/NoopZone **and Apple Health** data. No WHOOP cloud account is required for this path.

## What is implemented

- Authenticated `GET /api/noop` capability negotiation.
- Authenticated `POST /api/noop` NDJSON batches.
- gzip and identity request bodies.
- All 12 core NOOP Push 1.0 streams.
- NoopZone Apple Health extensions: `appleDaily`, `metricSeries`, and `appleStepHour`.
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

The NoopZone iOS endpoint is:

```text
http://NAS-IP:8700/api/noop
```

For access away from home, prefer HTTPS or Tailscale rather than exposing port 8700 directly to the internet.

## Backups

Back up the `data/` directory. The receiver state ID and all health records live in `data/noopzone.sqlite3`.

## Security

`NOOP_PUSH_TOKEN` authenticates writes from NoopZone. `DASHBOARD_TOKEN` is optional and protects JSON dashboard APIs when set. Keep both values out of git.

## Protocol

The receiver follows NOOP's documented Push Protocol 1.0 contract for the core registry. NoopZone adds three optional extension streams for Apple Health data; these are advertised separately in the capability response so a strict upstream NOOP client can ignore them. The phone remains authoritative; this server never sends health records or commands back to the phone.

## Attribution

The dashboard direction is inspired by the MIT-licensed [Vitals](https://github.com/DocStream-Oficial/vitals) self-hosted health project. This repository uses an independent minimal implementation focused on NOOP Push rather than Vitals' OAuth/cloud-source stack.

NOOP/NoopZone is independent and is not affiliated with WHOOP, Inc.
