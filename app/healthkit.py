from __future__ import annotations

import json
import math
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree as ET


APPLE_DEVICE_ID = "apple-health"
APPLE_SOURCE_ID = "healthkit-export"


def _date(value: str) -> datetime:
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S.%f %z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"unsupported Apple Health date: {value}") from exc


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _type_suffix(value: str) -> str:
    for prefix in (
        "HKQuantityTypeIdentifier",
        "HKCategoryTypeIdentifier",
        "HKCharacteristicTypeIdentifier",
    ):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _sport_name(value: str) -> str:
    value = value.removeprefix("HKWorkoutActivityType")
    if not value:
        return "Workout"
    return re.sub(r"(?<!^)(?=[A-Z])", " ", value).strip()


def _mass_kg(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit in {"lb", "lbs"}:
        return value * 0.45359237
    if unit == "g":
        return value / 1000.0
    return value


def _distance_m(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit in {"km", "kilometer", "kilometers"}:
        return value * 1000.0
    if unit in {"mi", "mile", "miles"}:
        return value * 1609.344
    if unit in {"ft", "foot", "feet"}:
        return value * 0.3048
    return value


def _energy_kcal(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit == "kj":
        return value * 0.239005736
    return value


def _temp_c(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit in {"degf", "°f"}:
        return (value - 32.0) * 5.0 / 9.0
    return value


def _percentage(value: float) -> float:
    return value * 100.0 if 0 < value <= 1.0 else value


def _glucose_mmol_l(value: float, unit: str) -> float:
    unit = unit.lower().replace(" ", "")
    if "mg/dl" in unit:
        return value / 18.0182
    return value


@dataclass
class _Mean:
    total: float = 0.0
    count: int = 0

    def add(self, value: float) -> None:
        self.total += value
        self.count += 1

    @property
    def value(self) -> float | None:
        return self.total / self.count if self.count else None


@dataclass
class _Sleep:
    start_ts: int | None = None
    end_ts: int | None = None
    core: float = 0.0
    deep: float = 0.0
    rem: float = 0.0
    awake: float = 0.0
    in_bed: float = 0.0
    unspecified: float = 0.0

    def add(self, stage: str, start: datetime, end: datetime) -> None:
        seconds = max(0.0, (end - start).total_seconds())
        minutes = seconds / 60.0
        self.start_ts = min(self.start_ts, int(start.timestamp())) if self.start_ts is not None else int(start.timestamp())
        self.end_ts = max(self.end_ts, int(end.timestamp())) if self.end_ts is not None else int(end.timestamp())
        if stage == "AsleepCore":
            self.core += minutes
        elif stage == "AsleepDeep":
            self.deep += minutes
        elif stage == "AsleepREM":
            self.rem += minutes
        elif stage == "Awake":
            self.awake += minutes
        elif stage == "InBed":
            self.in_bed += minutes
        elif stage in {"Asleep", "AsleepUnspecified"}:
            self.unspecified += minutes

    @property
    def asleep(self) -> float:
        staged = self.core + self.deep + self.rem
        return staged if staged > 0 else self.unspecified

    @property
    def score(self) -> float:
        return self.asleep


@dataclass
class HealthKitWorkout:
    start_ts: int
    end_ts: int
    sport: str
    source: str | None = None
    duration_s: float | None = None
    energy_kcal: float | None = None
    distance_m: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None


@dataclass
class HealthKitImportResult:
    metrics: dict[str, dict[str, float]]
    sleeps: list[dict]
    workouts: list[HealthKitWorkout]
    record_count: int
    relevant_record_count: int
    workout_count: int
    first_ts: int | None
    last_ts: int | None
    source_names: list[str]

    def summary(self) -> dict:
        return {
            "recordCount": self.record_count,
            "relevantRecordCount": self.relevant_record_count,
            "workoutCount": self.workout_count,
            "days": len(self.metrics),
            "sleepSessions": len(self.sleeps),
            "firstTs": self.first_ts,
            "lastTs": self.last_ts,
            "sources": self.source_names,
        }


class _Accumulator:
    def __init__(self) -> None:
        self.means: dict[tuple[str, str], _Mean] = defaultdict(_Mean)
        self.maxima: dict[tuple[str, str], float] = {}
        self.latest: dict[tuple[str, str], tuple[int, float]] = {}
        self.sums_by_source: dict[tuple[str, str, str], float] = defaultdict(float)
        self.sleeps: dict[tuple[str, str], _Sleep] = defaultdict(_Sleep)
        self.workouts: list[HealthKitWorkout] = []
        self.sources: set[str] = set()
        self.record_count = 0
        self.relevant_record_count = 0
        self.first_ts: int | None = None
        self.last_ts: int | None = None

    def _touch(self, start: datetime, end: datetime) -> None:
        s, e = int(start.timestamp()), int(end.timestamp())
        self.first_ts = min(self.first_ts, s) if self.first_ts is not None else s
        self.last_ts = max(self.last_ts, e) if self.last_ts is not None else e

    def mean(self, day: str, key: str, value: float) -> None:
        self.means[(day, key)].add(value)

    def maximum(self, day: str, key: str, value: float) -> None:
        old = self.maxima.get((day, key))
        self.maxima[(day, key)] = value if old is None else max(old, value)

    def latest_value(self, day: str, key: str, ts: int, value: float) -> None:
        old = self.latest.get((day, key))
        if old is None or ts >= old[0]:
            self.latest[(day, key)] = (ts, value)

    def source_sum(self, day: str, key: str, source: str, value: float) -> None:
        self.sums_by_source[(day, key, source)] += value

    def add_record(self, attrs: dict[str, str]) -> None:
        self.record_count += 1
        raw_type = attrs.get("type", "")
        kind = _type_suffix(raw_type)
        source = attrs.get("sourceName") or "unknown"
        self.sources.add(source)

        start_raw = attrs.get("startDate")
        end_raw = attrs.get("endDate") or start_raw
        if not start_raw or not end_raw:
            return
        try:
            start, end = _date(start_raw), _date(end_raw)
        except ValueError:
            return
        self._touch(start, end)
        day = end.date().isoformat()

        if kind == "SleepAnalysis":
            stage = attrs.get("value", "").removeprefix("HKCategoryValueSleepAnalysis")
            if stage in {"AsleepCore", "AsleepDeep", "AsleepREM", "Awake", "InBed", "Asleep", "AsleepUnspecified"}:
                self.sleeps[(day, source)].add(stage, start, end)
                self.relevant_record_count += 1
            return

        value = _float(attrs.get("value"))
        if value is None:
            return
        unit = attrs.get("unit", "")
        end_ts = int(end.timestamp())

        if kind == "HeartRate":
            self.mean(day, "avg_hr", value)
            self.maximum(day, "max_hr", value)
        elif kind == "RestingHeartRate":
            self.mean(day, "resting_hr", value)
        elif kind == "WalkingHeartRateAverage":
            self.mean(day, "walking_hr", value)
        elif kind == "HeartRateVariabilitySDNN":
            self.mean(day, "hrv", value)
        elif kind == "OxygenSaturation":
            self.mean(day, "spo2", _percentage(value))
        elif kind == "RespiratoryRate":
            self.mean(day, "resp_rate", value)
        elif kind == "VO2Max":
            self.latest_value(day, "vo2max", end_ts, value)
        elif kind == "BodyMass":
            self.latest_value(day, "weight", end_ts, _mass_kg(value, unit))
        elif kind == "BodyFatPercentage":
            self.latest_value(day, "body_fat", end_ts, _percentage(value))
        elif kind == "LeanBodyMass":
            self.latest_value(day, "lean_mass", end_ts, _mass_kg(value, unit))
        elif kind == "BodyMassIndex":
            self.latest_value(day, "bmi", end_ts, value)
        elif kind == "BodyTemperature":
            self.mean(day, "body_temp_c", _temp_c(value, unit))
        elif kind == "AppleSleepingWristTemperature":
            self.mean(day, "wrist_temp_c", _temp_c(value, unit))
        elif kind == "BloodPressureSystolic":
            self.mean(day, "systolic_bp", value)
        elif kind == "BloodPressureDiastolic":
            self.mean(day, "diastolic_bp", value)
        elif kind == "BloodGlucose":
            self.mean(day, "blood_glucose_mmol_l", _glucose_mmol_l(value, unit))
        elif kind == "StepCount":
            self.source_sum(day, "steps", source, value)
        elif kind == "ActiveEnergyBurned":
            self.source_sum(day, "active_kcal", source, _energy_kcal(value, unit))
        elif kind == "BasalEnergyBurned":
            self.source_sum(day, "basal_kcal", source, _energy_kcal(value, unit))
        elif kind == "DistanceWalkingRunning":
            self.source_sum(day, "walking_distance_m", source, _distance_m(value, unit))
        elif kind == "DistanceCycling":
            self.source_sum(day, "cycling_distance_m", source, _distance_m(value, unit))
        elif kind == "FlightsClimbed":
            self.source_sum(day, "flights_climbed", source, value)
        elif kind == "AppleExerciseTime":
            self.source_sum(day, "exercise_min", source, value)
        elif kind == "AppleStandTime":
            self.source_sum(day, "stand_min", source, value)
        else:
            return
        self.relevant_record_count += 1

    def add_workout(self, elem: ET.Element) -> None:
        attrs = elem.attrib
        start_raw = attrs.get("startDate")
        end_raw = attrs.get("endDate")
        if not start_raw or not end_raw:
            return
        try:
            start, end = _date(start_raw), _date(end_raw)
        except ValueError:
            return
        self._touch(start, end)
        source = attrs.get("sourceName")
        if source:
            self.sources.add(source)
        sport = _sport_name(attrs.get("workoutActivityType", "Workout"))
        duration = _float(attrs.get("duration"))
        if duration is not None:
            duration_unit = (attrs.get("durationUnit") or "min").lower()
            if duration_unit.startswith("min"):
                duration *= 60.0
            elif duration_unit.startswith("h"):
                duration *= 3600.0
        else:
            duration = max(0.0, (end - start).total_seconds())

        energy = _float(attrs.get("totalEnergyBurned"))
        if energy is not None:
            energy = _energy_kcal(energy, attrs.get("totalEnergyBurnedUnit", "kcal"))
        distance = _float(attrs.get("totalDistance"))
        if distance is not None:
            distance = _distance_m(distance, attrs.get("totalDistanceUnit", "m"))

        avg_hr = None
        max_hr = None
        for child in elem:
            if child.tag != "WorkoutStatistics":
                continue
            if _type_suffix(child.attrib.get("type", "")) != "HeartRate":
                continue
            avg_hr = _float(child.attrib.get("average"))
            max_hr = _float(child.attrib.get("maximum"))

        self.workouts.append(
            HealthKitWorkout(
                start_ts=int(start.timestamp()),
                end_ts=int(end.timestamp()),
                sport=sport,
                source=source,
                duration_s=duration,
                energy_kcal=energy,
                distance_m=distance,
                avg_hr=avg_hr,
                max_hr=max_hr,
            )
        )

    def finish(self) -> HealthKitImportResult:
        metrics: dict[str, dict[str, float]] = defaultdict(dict)

        for (day, key), agg in self.means.items():
            if agg.value is not None:
                metrics[day][key] = agg.value
        for (day, key), value in self.maxima.items():
            metrics[day][key] = value
        for (day, key), (_, value) in self.latest.items():
            metrics[day][key] = value

        grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
        for (day, key, _source), value in self.sums_by_source.items():
            grouped[(day, key)].append(value)
        for (day, key), values in grouped.items():
            if values:
                metrics[day][key] = max(values)

        sleeps: list[dict] = []
        by_day: dict[str, list[tuple[str, _Sleep]]] = defaultdict(list)
        for (day, source), sleep in self.sleeps.items():
            by_day[day].append((source, sleep))
        for day, choices in by_day.items():
            source, sleep = max(choices, key=lambda item: item[1].score)
            if sleep.start_ts is None or sleep.end_ts is None or sleep.asleep <= 0:
                continue
            metrics[day]["asleep_min"] = sleep.asleep
            metrics[day]["deep_min"] = sleep.deep
            metrics[day]["rem_min"] = sleep.rem
            metrics[day]["core_min"] = sleep.core
            metrics[day]["awake_min"] = sleep.awake
            metrics[day]["in_bed_min"] = sleep.in_bed
            sleeps.append(
                {
                    "day": day,
                    "startTs": sleep.start_ts,
                    "endTs": sleep.end_ts,
                    "source": source,
                    "asleepMin": sleep.asleep,
                    "deepMin": sleep.deep,
                    "remMin": sleep.rem,
                    "coreMin": sleep.core,
                    "awakeMin": sleep.awake,
                    "inBedMin": sleep.in_bed,
                }
            )

        return HealthKitImportResult(
            metrics={day: values for day, values in sorted(metrics.items())},
            sleeps=sorted(sleeps, key=lambda x: x["startTs"]),
            workouts=sorted(self.workouts, key=lambda x: x.start_ts),
            record_count=self.record_count,
            relevant_record_count=self.relevant_record_count,
            workout_count=len(self.workouts),
            first_ts=self.first_ts,
            last_ts=self.last_ts,
            source_names=sorted(self.sources),
        )


def _xml_stream(path: Path, max_uncompressed_bytes: int) -> tuple[BinaryIO, zipfile.ZipFile | None]:
    if zipfile.is_zipfile(path):
        archive = zipfile.ZipFile(path)
        candidates = [
            info for info in archive.infolist()
            if Path(info.filename).name == "export.xml" and not info.is_dir()
        ]
        if not candidates:
            archive.close()
            raise ValueError("Apple Health ZIP does not contain export.xml")
        info = max(candidates, key=lambda item: item.file_size)
        if info.file_size > max_uncompressed_bytes:
            archive.close()
            raise ValueError("Apple Health export.xml exceeds the configured uncompressed-size limit")
        return archive.open(info, "r"), archive
    if path.stat().st_size > max_uncompressed_bytes:
        raise ValueError("Apple Health XML exceeds the configured size limit")
    return path.open("rb"), None


def parse_healthkit_export(path: str | Path, max_uncompressed_bytes: int = 8 * 1024**3) -> HealthKitImportResult:
    source, archive = _xml_stream(Path(path), max_uncompressed_bytes)
    acc = _Accumulator()
    try:
        for _event, elem in ET.iterparse(source, events=("end",)):
            if elem.tag == "Record":
                acc.add_record(elem.attrib)
                elem.clear()
            elif elem.tag == "Workout":
                acc.add_workout(elem)
                elem.clear()
    finally:
        source.close()
        if archive is not None:
            archive.close()
    return acc.finish()


def result_to_json(result: HealthKitImportResult) -> str:
    return json.dumps(
        {
            "metrics": result.metrics,
            "sleeps": result.sleeps,
            "workouts": [w.__dict__ for w in result.workouts],
            "summary": result.summary(),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
