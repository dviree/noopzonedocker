from __future__ import annotations

import asyncio
import gzip
import hashlib
import ipaddress
import json
import os
import secrets
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.healthkit import APPLE_DEVICE_ID, APPLE_SOURCE_ID, HealthKitImportResult, parse_healthkit_export

PROTOCOL_VERSION = "1.0"
MAX_DECODED = 4 * 1024 * 1024
STREAMS = {
    "hrSample": "append",
    "rrInterval": "append",
    "event": "append",
    "battery": "append",
    "spo2Sample": "append",
    "skinTempSample": "append",
    "respSample": "append",
    "gravitySample": "append",
    "dailyMetric": "replace_window",
    "sleepSession": "replace_window",
    "workout": "replace_window",
    "journal": "replace_window",
}
# NoopZone extensions. Kept out of the core `streams` capability list so an upstream
# NOOP Push 1.0 client still sees a strict registry; NoopZone iOS opts into these explicitly.
EXTENSION_STREAMS = {
    "appleDaily": "replace_window",
    "metricSeries": "replace_window",
    "appleStepHour": "append",
}
ALL_STREAMS = STREAMS | EXTENSION_STREAMS

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "noopzone.sqlite3"
PUSH_TOKEN = os.getenv("NOOP_PUSH_TOKEN", "")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")
HEALTHKIT_MAX_UPLOAD_MB = max(1, int(os.getenv("HEALTHKIT_MAX_UPLOAD_MB", "2048")))
HEALTHKIT_MAX_XML_GB = max(1, int(os.getenv("HEALTHKIT_MAX_XML_GB", "8")))

app = FastAPI(title="NoopZone Docker", version="0.1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
    finally:
        con.close()


def init_db() -> None:
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
              source_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              stream TEXT NOT NULL,
              natural_key TEXT NOT NULL,
              key_json TEXT NOT NULL,
              data_json TEXT NOT NULL,
              selector_text TEXT,
              selector_int INTEGER,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY(source_id, device_id, stream, natural_key)
            );
            CREATE INDEX IF NOT EXISTS records_stream_idx
              ON records(stream, device_id);
            CREATE INDEX IF NOT EXISTS records_selector_text_idx
              ON records(source_id, device_id, stream, selector_text);
            CREATE INDEX IF NOT EXISTS records_selector_int_idx
              ON records(source_id, device_id, stream, selector_int);

            CREATE TABLE IF NOT EXISTS batch_receipts (
              source_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              batch_id TEXT NOT NULL,
              body_sha256 TEXT NOT NULL,
              ack_json TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              PRIMARY KEY(source_id, device_id, batch_id)
            );

            CREATE TABLE IF NOT EXISTS replacement_generation (
              source_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              stream TEXT NOT NULL,
              replacement_id TEXT NOT NULL,
              PRIMARY KEY(source_id, device_id, stream)
            );
            CREATE TABLE IF NOT EXISTS superseded_replacement (
              source_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              stream TEXT NOT NULL,
              replacement_id TEXT NOT NULL,
              PRIMARY KEY(source_id, device_id, stream, replacement_id)
            );
            CREATE TABLE IF NOT EXISTS healthkit_imports (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              imported_at INTEGER NOT NULL,
              file_name TEXT,
              upload_bytes INTEGER NOT NULL,
              summary_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS replacement_part (
              source_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              stream TEXT NOT NULL,
              replacement_id TEXT NOT NULL,
              part INTEGER NOT NULL,
              parts INTEGER NOT NULL,
              selector TEXT NOT NULL,
              start_value TEXT NOT NULL,
              end_value TEXT NOT NULL,
              records_json TEXT NOT NULL,
              batch_id TEXT NOT NULL,
              PRIMARY KEY(source_id, device_id, stream, replacement_id, part)
            );
            """
        )
        row = con.execute("SELECT value FROM meta WHERE key='receiver_state_id'").fetchone()
        if not row:
            con.execute(
                "INSERT INTO meta(key,value) VALUES('receiver_state_id',?)",
                (str(uuid.uuid4()).lower(),),
            )


@app.on_event("startup")
def startup() -> None:
    init_db()


def error(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"type": "error", "protocolVersion": PROTOCOL_VERSION, "code": code})


def bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        return ""
    return authorization[7:]


def require_push_auth(authorization: str | None) -> None:
    if not PUSH_TOKEN:
        raise error(503, "push_token_not_configured")
    if not secrets.compare_digest(bearer(authorization), PUSH_TOKEN):
        raise error(401, "unauthorized")


def require_dashboard_auth(authorization: str | None) -> None:
    if not DASHBOARD_TOKEN:
        return
    if not secrets.compare_digest(bearer(authorization), DASHBOARD_TOKEN):
        raise HTTPException(status_code=401, detail="dashboard authentication required")


def require_local_import(request: Request) -> None:
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise HTTPException(status_code=403, detail="Apple Health import is limited to local or Tailscale clients")
    # Allow loopback, RFC1918/private/link-local and Tailscale CGNAT 100.64.0.0/10.
    tailscale = ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")
    if not (ip.is_loopback or ip.is_private or ip.is_link_local or tailscale):
        raise HTTPException(status_code=403, detail="Apple Health import is limited to local or Tailscale clients")


def receiver_state_id() -> str:
    with db() as con:
        return con.execute("SELECT value FROM meta WHERE key='receiver_state_id'").fetchone()["value"]


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and exc.detail.get("type") == "error":
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.get("/healthz")
def healthz():
    return {"status": "ok", "protocolVersion": PROTOCOL_VERSION}


@app.get("/api/noop")
def noop_capabilities(
    authorization: str | None = Header(default=None),
    noop_push_accept_version: str | None = Header(default=None, alias="NOOP-Push-Accept-Version"),
):
    require_push_auth(authorization)
    offered = [x.strip() for x in (noop_push_accept_version or "").split(",") if x.strip()]
    if PROTOCOL_VERSION not in offered:
        raise error(406, "unsupported_version")
    return {
        "type": "capabilities",
        "protocolVersion": PROTOCOL_VERSION,
        "receiverStateId": receiver_state_id(),
        "streams": list(STREAMS.keys()),
        "extensions": list(EXTENSION_STREAMS.keys()),
    }


def decode_body(raw: bytes, encoding: str | None) -> bytes:
    if encoding and encoding.lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            raise error(400, "invalid_gzip")
    elif encoding and encoding.lower() not in ("identity", ""):
        raise error(415, "unsupported_content_encoding")
    if len(raw) > MAX_DECODED:
        raise error(413, "body_too_large")
    return raw


def parse_ndjson(raw: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise error(400, "invalid_utf8")
    if not text.endswith("\n"):
        raise error(400, "missing_final_newline")
    lines = text.splitlines()
    if not lines:
        raise error(400, "empty_batch")
    try:
        docs = [json.loads(line) for line in lines]
    except Exception:
        raise error(400, "invalid_ndjson")
    header, records = docs[0], docs[1:]
    if not isinstance(header, dict) or header.get("type") != "batch":
        raise error(400, "invalid_header")
    if any(not isinstance(r, dict) or r.get("type") != "record" for r in records):
        raise error(400, "invalid_record")
    return header, records


def canonical_key(key: dict[str, Any]) -> str:
    return json.dumps(key, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_header(header: dict[str, Any], records: list[dict[str, Any]]) -> None:
    if header.get("protocolVersion") != PROTOCOL_VERSION:
        raise error(422, "unsupported_version")
    stream = header.get("stream")
    if stream not in ALL_STREAMS:
        raise error(422, "unsupported_stream")
    if header.get("delivery") != ALL_STREAMS[stream]:
        raise error(422, "delivery_mismatch")
    for k in ("batchId", "sourceId", "deviceId"):
        if not isinstance(header.get(k), str) or not header[k]:
            raise error(400, "invalid_header")
    if header.get("recordCount") != len(records) or len(records) > 5000:
        raise error(400, "record_count_mismatch")
    for r in records:
        if not isinstance(r.get("key"), dict) or not isinstance(r.get("data"), dict):
            raise error(400, "invalid_record")


def make_ack(header: dict[str, Any], count: int) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "batchId": header["batchId"],
        "stream": header["stream"],
        "deviceId": header["deviceId"],
        "endCursor": header.get("endCursor"),
        "acceptedRows": count,
        "status": "accepted",
    }


def selector_for_record(selector: str | None, key: dict[str, Any]) -> tuple[str | None, int | None]:
    if selector == "day":
        v = key.get("day")
        if not isinstance(v, str):
            raise error(422, "invalid_selector")
        return v, None
    if selector == "startTs":
        v = key.get("startTs")
        if not isinstance(v, int):
            raise error(422, "invalid_selector")
        return None, v
    return None, None


def upsert_records(con: sqlite3.Connection, header: dict[str, Any], records: list[dict[str, Any]], selector: str | None = None) -> None:
    now = int(time.time())
    seen: set[str] = set()
    for r in records:
        key = r["key"]
        nk = canonical_key(key)
        if nk in seen:
            raise error(422, "duplicate_key")
        seen.add(nk)
        st, si = selector_for_record(selector, key)
        con.execute(
            """
            INSERT INTO records(source_id,device_id,stream,natural_key,key_json,data_json,selector_text,selector_int,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id,device_id,stream,natural_key) DO UPDATE SET
              key_json=excluded.key_json,
              data_json=excluded.data_json,
              selector_text=excluded.selector_text,
              selector_int=excluded.selector_int,
              updated_at=excluded.updated_at
            """,
            (
                header["sourceId"], header["deviceId"], header["stream"], nk,
                json.dumps(key, separators=(",", ":"), ensure_ascii=False),
                json.dumps(r["data"], separators=(",", ":"), ensure_ascii=False),
                st, si, now,
            ),
        )


def handle_append(con: sqlite3.Connection, header: dict[str, Any], records: list[dict[str, Any]]) -> None:
    if not records:
        raise error(400, "empty_append")
    upsert_records(con, header, records)


def replacement_meta(header: dict[str, Any]) -> tuple[str, str, str, str, int, int]:
    w = header.get("window")
    if not isinstance(w, dict):
        raise error(400, "missing_window")
    rid = w.get("replacementId")
    selector = w.get("selector")
    start = w.get("startInclusive")
    end = w.get("endExclusive")
    part = w.get("part")
    parts = w.get("parts")
    if not isinstance(rid, str) or selector not in ("day", "startTs"):
        raise error(400, "invalid_window")
    if not isinstance(part, int) or not isinstance(parts, int) or part < 1 or parts < 1 or part > parts:
        raise error(400, "invalid_window")
    if selector == "day":
        if not isinstance(start, str) or not isinstance(end, str) or start >= end:
            raise error(400, "invalid_window")
    else:
        if not isinstance(start, int) or not isinstance(end, int) or start >= end:
            raise error(400, "invalid_window")
    return rid, selector, str(start), str(end), part, parts


def handle_replace(con: sqlite3.Connection, header: dict[str, Any], records: list[dict[str, Any]]) -> None:
    rid, selector, start, end, part, parts = replacement_meta(header)
    scope = (header["sourceId"], header["deviceId"], header["stream"])

    if con.execute(
        "SELECT 1 FROM superseded_replacement WHERE source_id=? AND device_id=? AND stream=? AND replacement_id=?",
        (*scope, rid),
    ).fetchone():
        raise error(409, "superseded_replacement")

    current = con.execute(
        "SELECT replacement_id FROM replacement_generation WHERE source_id=? AND device_id=? AND stream=?",
        scope,
    ).fetchone()
    if current and current["replacement_id"] != rid:
        old = current["replacement_id"]
        con.execute(
            "INSERT OR IGNORE INTO superseded_replacement VALUES(?,?,?,?)",
            (*scope, old),
        )
        con.execute(
            "DELETE FROM replacement_part WHERE source_id=? AND device_id=? AND stream=? AND replacement_id=?",
            (*scope, old),
        )
        con.execute(
            "UPDATE replacement_generation SET replacement_id=? WHERE source_id=? AND device_id=? AND stream=?",
            (rid, *scope),
        )
    elif not current:
        con.execute(
            "INSERT INTO replacement_generation(source_id,device_id,stream,replacement_id) VALUES(?,?,?,?)",
            (*scope, rid),
        )

    existing = con.execute(
        """SELECT parts,selector,start_value,end_value,records_json,batch_id FROM replacement_part
           WHERE source_id=? AND device_id=? AND stream=? AND replacement_id=? AND part=?""",
        (*scope, rid, part),
    ).fetchone()
    records_json = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if existing:
        if (existing["parts"] != parts or existing["selector"] != selector or
            existing["start_value"] != start or existing["end_value"] != end or
            existing["records_json"] != records_json or existing["batch_id"] != header["batchId"]):
            raise error(409, "replacement_part_conflict")
    else:
        con.execute(
            """INSERT INTO replacement_part(source_id,device_id,stream,replacement_id,part,parts,selector,start_value,end_value,records_json,batch_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (*scope, rid, part, parts, selector, start, end, records_json, header["batchId"]),
        )

    staged = con.execute(
        """SELECT part,parts,selector,start_value,end_value,records_json FROM replacement_part
           WHERE source_id=? AND device_id=? AND stream=? AND replacement_id=? ORDER BY part""",
        (*scope, rid),
    ).fetchall()
    if len(staged) != parts:
        return
    if {r["parts"] for r in staged} != {parts} or {r["selector"] for r in staged} != {selector} or {r["start_value"] for r in staged} != {start} or {r["end_value"] for r in staged} != {end}:
        raise error(409, "replacement_window_conflict")
    if [r["part"] for r in staged] != list(range(1, parts + 1)):
        return

    merged: list[dict[str, Any]] = []
    for row in staged:
        merged.extend(json.loads(row["records_json"]))

    if selector == "day":
        con.execute(
            """DELETE FROM records WHERE source_id=? AND device_id=? AND stream=?
               AND selector_text>=? AND selector_text<?""",
            (*scope, start, end),
        )
    else:
        con.execute(
            """DELETE FROM records WHERE source_id=? AND device_id=? AND stream=?
               AND selector_int>=? AND selector_int<?""",
            (*scope, int(start), int(end)),
        )
    upsert_records(con, header, merged, selector)
    con.execute(
        "DELETE FROM replacement_part WHERE source_id=? AND device_id=? AND stream=? AND replacement_id=?",
        (*scope, rid),
    )


def _put_record(
    con: sqlite3.Connection,
    *,
    stream: str,
    key: dict[str, Any],
    data: dict[str, Any],
    selector_text: str | None = None,
    selector_int: int | None = None,
    now: int,
) -> None:
    con.execute(
        """
        INSERT INTO records(source_id,device_id,stream,natural_key,key_json,data_json,selector_text,selector_int,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_id,device_id,stream,natural_key) DO UPDATE SET
          key_json=excluded.key_json,
          data_json=excluded.data_json,
          selector_text=excluded.selector_text,
          selector_int=excluded.selector_int,
          updated_at=excluded.updated_at
        """,
        (
            APPLE_SOURCE_ID,
            APPLE_DEVICE_ID,
            stream,
            canonical_key(key),
            json.dumps(key, separators=(",", ":"), ensure_ascii=False),
            json.dumps(data, separators=(",", ":"), ensure_ascii=False),
            selector_text,
            selector_int,
            now,
        ),
    )


def persist_healthkit(result: HealthKitImportResult, file_name: str | None, upload_bytes: int) -> dict[str, Any]:
    now = int(time.time())
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            # Apple Health export is a snapshot. Replace only the previous export-derived rows;
            # NOOP/WHOOP data and any future live source remain untouched.
            con.execute(
                "DELETE FROM records WHERE source_id=? AND device_id=?",
                (APPLE_SOURCE_ID, APPLE_DEVICE_ID),
            )

            for day, metrics in result.metrics.items():
                for key, value in metrics.items():
                    _put_record(
                        con,
                        stream="metricSeries",
                        key={"day": day, "key": key},
                        data={"value": value},
                        selector_text=day,
                        now=now,
                    )

                _put_record(
                    con,
                    stream="appleDaily",
                    key={"day": day},
                    data={
                        "steps": metrics.get("steps"),
                        "activeKcal": metrics.get("active_kcal"),
                        "basalKcal": metrics.get("basal_kcal"),
                        "vo2max": metrics.get("vo2max"),
                        "avgHr": metrics.get("avg_hr"),
                        "maxHr": metrics.get("max_hr"),
                        "walkingHr": metrics.get("walking_hr"),
                        "weightKg": metrics.get("weight"),
                    },
                    selector_text=day,
                    now=now,
                )

                _put_record(
                    con,
                    stream="dailyMetric",
                    key={"day": day},
                    data={
                        "totalSleepMin": metrics.get("asleep_min"),
                        "efficiency": None,
                        "deepMin": metrics.get("deep_min"),
                        "remMin": metrics.get("rem_min"),
                        "lightMin": metrics.get("core_min"),
                        "disturbances": None,
                        "restingHr": metrics.get("resting_hr"),
                        "avgHrv": metrics.get("hrv"),
                        "recovery": None,
                        "strain": None,
                        "exerciseCount": None,
                        "spo2Pct": metrics.get("spo2"),
                        "skinTempDevC": metrics.get("wrist_temp_c") or metrics.get("body_temp_c"),
                        "respRateBpm": metrics.get("resp_rate"),
                    },
                    selector_text=day,
                    now=now,
                )

            for sleep in result.sleeps:
                _put_record(
                    con,
                    stream="sleepSession",
                    key={"startTs": sleep["startTs"]},
                    data={
                        "endTs": sleep["endTs"],
                        "efficiency": None,
                        "restingHr": result.metrics.get(sleep["day"], {}).get("resting_hr"),
                        "avgHrv": result.metrics.get(sleep["day"], {}).get("hrv"),
                        "stagesJSON": json.dumps(
                            {
                                "asleepMin": sleep["asleepMin"],
                                "deepMin": sleep["deepMin"],
                                "remMin": sleep["remMin"],
                                "coreMin": sleep["coreMin"],
                                "awakeMin": sleep["awakeMin"],
                                "inBedMin": sleep["inBedMin"],
                            },
                            separators=(",", ":"),
                        ),
                        "userEdited": False,
                        "source": sleep["source"],
                    },
                    selector_int=sleep["startTs"],
                    now=now,
                )

            for workout in result.workouts:
                _put_record(
                    con,
                    stream="workout",
                    key={"startTs": workout.start_ts, "sport": workout.sport},
                    data={
                        "endTs": workout.end_ts,
                        "source": workout.source or "Apple Health",
                        "durationS": workout.duration_s,
                        "energyKcal": workout.energy_kcal,
                        "avgHr": workout.avg_hr,
                        "maxHr": workout.max_hr,
                        "strain": None,
                        "distanceM": workout.distance_m,
                        "zonesJSON": None,
                        "notes": None,
                    },
                    selector_int=workout.start_ts,
                    now=now,
                )

            summary = result.summary()
            con.execute(
                "INSERT INTO healthkit_imports(imported_at,file_name,upload_bytes,summary_json) VALUES(?,?,?,?)",
                (now, file_name, upload_bytes, json.dumps(summary, separators=(",", ":"), ensure_ascii=False)),
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return summary


@app.post("/api/healthkit/import")
async def import_healthkit(
    request: Request,
    file: UploadFile = File(...),
):
    require_local_import(request)
    max_upload = HEALTHKIT_MAX_UPLOAD_MB * 1024 * 1024
    suffix = ".zip" if (file.filename or "").lower().endswith(".zip") else ".xml"
    total = 0
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="noopzone-healthkit-", suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_upload:
                    raise HTTPException(status_code=413, detail="Apple Health upload exceeds configured limit")
                tmp.write(chunk)

        try:
            result = await asyncio.to_thread(
                parse_healthkit_export,
                tmp_path,
                HEALTHKIT_MAX_XML_GB * 1024**3,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        summary = await asyncio.to_thread(persist_healthkit, result, file.filename, total)
        return {"status": "imported", **summary}
    finally:
        await file.close()
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


@app.get("/api/healthkit/imports")
def healthkit_imports(authorization: str | None = Header(default=None)):
    require_dashboard_auth(authorization)
    with db() as con:
        rows = con.execute(
            "SELECT imported_at,file_name,upload_bytes,summary_json FROM healthkit_imports ORDER BY id DESC LIMIT 20"
        ).fetchall()
    return [
        {
            "importedAt": row["imported_at"],
            "fileName": row["file_name"],
            "uploadBytes": row["upload_bytes"],
            "summary": json.loads(row["summary_json"]),
        }
        for row in rows
    ]


@app.post("/api/noop")
async def noop_push(
    request: Request,
    authorization: str | None = Header(default=None),
    content_encoding: str | None = Header(default=None),
):
    require_push_auth(authorization)
    raw = decode_body(await request.body(), content_encoding)
    header, records = parse_ndjson(raw)
    validate_header(header, records)
    body_hash = hashlib.sha256(raw).hexdigest()
    ack = make_ack(header, len(records))

    with db() as con:
        prior = con.execute(
            "SELECT body_sha256,ack_json FROM batch_receipts WHERE source_id=? AND device_id=? AND batch_id=?",
            (header["sourceId"], header["deviceId"], header["batchId"]),
        ).fetchone()
        if prior:
            if prior["body_sha256"] != body_hash:
                raise error(409, "batch_id_conflict")
            return JSONResponse(content=json.loads(prior["ack_json"]))

        try:
            con.execute("BEGIN IMMEDIATE")
            if header["delivery"] == "append":
                handle_append(con, header, records)
            else:
                handle_replace(con, header, records)
            con.execute(
                "INSERT INTO batch_receipts VALUES(?,?,?,?,?,?)",
                (
                    header["sourceId"], header["deviceId"], header["batchId"],
                    body_hash, json.dumps(ack, separators=(",", ":")), int(time.time()),
                ),
            )
            con.execute("COMMIT")
        except HTTPException:
            con.execute("ROLLBACK")
            raise
        except Exception:
            con.execute("ROLLBACK")
            raise
    return ack


def read_stream(stream: str, limit: int = 5000) -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute(
            """SELECT device_id,key_json,data_json,updated_at
               FROM records WHERE stream=?
               ORDER BY selector_text DESC, selector_int DESC, updated_at DESC
               LIMIT ?""",
            (stream, limit),
        ).fetchall()
    return [
        {
            "deviceId": r["device_id"],
            "key": json.loads(r["key_json"]),
            "data": json.loads(r["data_json"]),
            "updatedAt": r["updated_at"],
        }
        for r in rows
    ]


@app.get("/api/dashboard/summary")
def dashboard_summary(authorization: str | None = Header(default=None)):
    require_dashboard_auth(authorization)
    daily = read_stream("dailyMetric", 300)
    daily.sort(key=lambda r: (r["key"].get("day", ""), r["updatedAt"]))
    # Prefer a row that actually carries NOOP's recovery/strain for the hero. Apple Health also
    # writes dailyMetric rows, but those intentionally have recovery/strain = null.
    scored = [r for r in daily if r["data"].get("recovery") is not None or r["data"].get("strain") is not None]
    latest = scored[-1] if scored else (daily[-1] if daily else None)
    with db() as con:
        counts = {
            row["stream"]: row["n"]
            for row in con.execute("SELECT stream,COUNT(*) AS n FROM records GROUP BY stream")
        }
        last = con.execute("SELECT MAX(updated_at) AS ts FROM records").fetchone()["ts"]
    return {"latest": latest, "daily": daily, "counts": counts, "lastSync": last}


@app.get("/api/dashboard/sleep")
def dashboard_sleep(authorization: str | None = Header(default=None)):
    require_dashboard_auth(authorization)
    rows = read_stream("sleepSession", 100)
    rows.sort(key=lambda r: r["key"].get("startTs", 0), reverse=True)
    return rows


@app.get("/api/dashboard/workouts")
def dashboard_workouts(authorization: str | None = Header(default=None)):
    require_dashboard_auth(authorization)
    rows = read_stream("workout", 100)
    rows.sort(key=lambda r: r["key"].get("startTs", 0), reverse=True)
    return rows


@app.get("/api/dashboard/hr")
def dashboard_hr(authorization: str | None = Header(default=None), limit: int = 1440):
    require_dashboard_auth(authorization)
    limit = max(60, min(limit, 10000))
    rows = read_stream("hrSample", limit)
    rows.sort(key=lambda r: r["key"].get("ts", 0))
    return rows


@app.get("/api/dashboard/apple")
def dashboard_apple(authorization: str | None = Header(default=None)):
    require_dashboard_auth(authorization)
    daily = read_stream("appleDaily", 120)
    daily.sort(key=lambda r: r["key"].get("day", ""))
    metrics = read_stream("metricSeries", 3000)
    metrics = [r for r in metrics if r["deviceId"] == "apple-health"]
    metrics.sort(key=lambda r: (r["key"].get("day", ""), r["key"].get("key", "")))
    hourly_steps = read_stream("appleStepHour", 24 * 14)
    hourly_steps = [r for r in hourly_steps if r["deviceId"] == "apple-health"]
    hourly_steps.sort(key=lambda r: r["key"].get("ts", 0))
    return {
        "latest": daily[-1] if daily else None,
        "daily": daily,
        "metrics": metrics,
        "hourlySteps": hourly_steps,
    }
