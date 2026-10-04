from pathlib import Path
import zipfile

from app.healthkit import parse_healthkit_export


XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData>
  <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" value="3000" startDate="2026-10-01 08:00:00 +0200" endDate="2026-10-01 18:00:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Apple Watch" unit="count" value="5200" startDate="2026-10-01 08:00:00 +0200" endDate="2026-10-01 18:00:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" sourceName="Apple Watch" unit="ms" value="48" startDate="2026-10-01 07:00:00 +0200" endDate="2026-10-01 07:00:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierOxygenSaturation" sourceName="Apple Watch" unit="%" value="0.975" startDate="2026-10-01 07:00:00 +0200" endDate="2026-10-01 07:00:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="Scale" unit="kg" value="88.4" startDate="2026-10-01 07:30:00 +0200" endDate="2026-10-01 07:30:00 +0200"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Apple Watch" value="HKCategoryValueSleepAnalysisAsleepCore" startDate="2026-09-30 23:00:00 +0200" endDate="2026-10-01 01:00:00 +0200"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Apple Watch" value="HKCategoryValueSleepAnalysisAsleepDeep" startDate="2026-10-01 01:00:00 +0200" endDate="2026-10-01 02:00:00 +0200"/>
  <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Apple Watch" value="HKCategoryValueSleepAnalysisAsleepREM" startDate="2026-10-01 02:00:00 +0200" endDate="2026-10-01 03:00:00 +0200"/>
  <Workout workoutActivityType="HKWorkoutActivityTypeRunning" sourceName="Apple Watch" duration="30" durationUnit="min" totalDistance="5" totalDistanceUnit="km" totalEnergyBurned="350" totalEnergyBurnedUnit="kcal" startDate="2026-10-01 18:00:00 +0200" endDate="2026-10-01 18:30:00 +0200">
    <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="155" maximum="181" unit="count/min"/>
  </Workout>
</HealthData>
"""


def test_parse_xml(tmp_path: Path):
    p = tmp_path / "export.xml"
    p.write_text(XML)
    result = parse_healthkit_export(p)

    day = result.metrics["2026-10-01"]
    assert day["steps"] == 5200
    assert day["hrv"] == 48
    assert round(day["spo2"], 1) == 97.5
    assert day["weight"] == 88.4
    assert day["asleep_min"] == 240
    assert day["deep_min"] == 60
    assert day["rem_min"] == 60

    assert len(result.sleeps) == 1
    assert len(result.workouts) == 1
    workout = result.workouts[0]
    assert workout.sport == "Running"
    assert workout.duration_s == 1800
    assert workout.distance_m == 5000
    assert workout.energy_kcal == 350
    assert workout.avg_hr == 155
    assert workout.max_hr == 181


def test_parse_zip(tmp_path: Path):
    p = tmp_path / "export.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("apple_health_export/export.xml", XML)
    result = parse_healthkit_export(p)
    assert result.metrics["2026-10-01"]["steps"] == 5200
    assert result.summary()["workoutCount"] == 1
