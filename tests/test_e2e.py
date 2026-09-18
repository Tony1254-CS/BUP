"""
End-to-end API tests using the FastAPI test client.

Uses SKIP_LLM=true so no Gemini calls are made — tests the full
API contract (schema validation, optimizer, totals computation)
without LLM dependency.
"""
import json
import os
import pathlib

import pytest
from fastapi.testclient import TestClient

# Force LLM skip for e2e tests
os.environ["SKIP_LLM"] = "true"

from app.main import app

client = TestClient(app)

CASES_PATH = pathlib.Path(__file__).resolve().parent.parent / "sample_cases.json"


def _load_cases():
    with open(CASES_PATH) as f:
        pack = json.load(f)
    return pack["cases"]


CASES = _load_cases()


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_schema_validation_empty_notes():
    """operator_notes must have 1-3 entries."""
    resp = client.post("/optimize-energy", json={
        "scenario_id": "TEST",
        "operator_notes": [],
        "hours": [],
        "battery": {
            "capacity_kwh": 500,
            "initial_energy_kwh": 250,
            "minimum_energy_kwh": 50,
            "max_charge_kwh_per_hour": 100,
            "max_discharge_kwh_per_hour": 100,
        },
    })
    assert resp.status_code == 422  # Pydantic validation error


def test_schema_validation_missing_field():
    """Missing scenario_id should fail validation."""
    resp = client.post("/optimize-energy", json={
        "operator_notes": ["test"],
        "hours": [],
        "battery": {
            "capacity_kwh": 500,
            "initial_energy_kwh": 250,
            "minimum_energy_kwh": 50,
            "max_charge_kwh_per_hour": 100,
            "max_discharge_kwh_per_hour": 100,
        },
    })
    assert resp.status_code == 422


@pytest.fixture(params=CASES[:3], ids=[c["id"] for c in CASES[:3]])
def case(request):
    """First 3 cases for e2e (SKIP_LLM → directives will be no_op)."""
    return request.param


def test_e2e_response_structure(case):
    """With SKIP_LLM=true, all notes become no_op but the response schema is correct."""
    resp = client.post("/optimize-energy", json=case["input"])
    assert resp.status_code == 200
    body = resp.json()
    # Check all required top-level fields
    assert "scenario_id" in body
    assert "directive_interpretation" in body
    assert "hourly_plan" in body
    assert "total_grid_kwh" in body
    assert "total_cost_bdt" in body
    assert "peak_grid_kwh" in body
    assert "plan_summary" in body
    # Check lengths
    assert len(body["hourly_plan"]) == 24
    assert len(body["directive_interpretation"]) == len(case["input"]["operator_notes"])
    # All directives should be no_op since LLM is skipped
    for d in body["directive_interpretation"]:
        assert d["directive_type"] == "no_op"
        assert d["applies"] is False


def test_e2e_totals_consistent(case):
    """Verify total_grid_kwh and total_cost_bdt are consistent with hourly_plan."""
    resp = client.post("/optimize-energy", json=case["input"])
    body = resp.json()
    plan = body["hourly_plan"]
    hours = case["input"]["hours"]
    total_grid = round(sum(p["grid_kwh"] for p in plan), 2)
    total_cost = round(sum(p["grid_kwh"] * hours[p["hour"]]["tariff_bdt_per_kwh"] for p in plan), 2)
    peak = max(p["grid_kwh"] for p in plan)
    assert abs(body["total_grid_kwh"] - total_grid) < 0.1
    assert abs(body["total_cost_bdt"] - total_cost) < 0.1
    assert abs(body["peak_grid_kwh"] - peak) < 0.1
