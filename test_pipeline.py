import json
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.schemas import HourEntry, BatteryConfig, OptimizeResponse
from app.verifier import verify_schedule_compliance

client = TestClient(app)

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    print("\n[GET /health] Passed with {'status': 'ok'}")

def test_malformed_request_handling():
    # Structurally invalid payload
    bad_payload = {"scenario_id": "TEST-BAD", "invalid_key": 123}
    response = client.post("/optimize-energy", json=bad_payload)
    assert response.status_code == 400
    print("[POST /optimize-energy (malformed)] Successfully returned 400 Bad Request")

def test_all_10_sample_cases():
    with open("sample_cases.json", "r") as f:
        pack = json.load(f)

    cases = pack["cases"]
    print(f"\n==================================================")
    print(f"Executing End-to-End Evaluation on {len(cases)} Cases")
    print(f"==================================================")

    all_passed = True
    total_score = 0.0

    for case in cases:
        cid = case["id"]
        label = case["label"]
        payload = case["input"]
        expected = case["expected_output"]

        # Call the live endpoint
        response = client.post("/optimize-energy", json=payload)
        assert response.status_code == 200, f"{cid} failed with status {response.status_code}"

        res_data = response.json()
        parsed_res = OptimizeResponse(**res_data)

        # 1. Check directive interpretation
        exp_dirs = expected["directive_interpretation"]
        assert len(parsed_res.directive_interpretation) == len(exp_dirs)
        dir_match = True
        for team_d, exp_d in zip(parsed_res.directive_interpretation, exp_dirs):
            if (team_d.directive_type != exp_d["directive_type"] or
                team_d.applies != exp_d["applies"] or
                team_d.structured_adjustment != exp_d["structured_adjustment"]):
                dir_match = False
                break

        # 2. Independent Judge Replay Verification
        hours = [HourEntry(**h) for h in payload["hours"]]
        battery = BatteryConfig(**payload["battery"])
        is_valid, violations = verify_schedule_compliance(
            parsed_res,
            hours,
            battery,
            parsed_res.directive_interpretation
        )

        # 3. Cost optimality check
        team_cost = parsed_res.total_cost_bdt
        exp_cost = expected["total_cost_bdt"]
        cost_diff = abs(team_cost - exp_cost)
        optimal = cost_diff <= 0.05

        status = "PASS" if (dir_match and is_valid and optimal) else "FAIL"
        if status == "FAIL":
            all_passed = False

        print(f"[{status}] {cid} ({label})")
        print(f"       Directives Match: {dir_match}")
        print(f"       Replay Valid: {is_valid} ({len(violations)} violations)")
        if violations:
            for v in violations[:3]:
                print(f"         - {v}")
        print(f"       Cost: Team={team_cost:.2f} BDT | Expected={exp_cost:.2f} BDT | Diff={cost_diff:.2f}")
        print(f"       Grid: Team={parsed_res.total_grid_kwh:.2f} kWh | Expected={expected['total_grid_kwh']:.2f} kWh")

    print("\n==================================================")
    if all_passed:
        print("ALL 10 PUBLIC TEST CASES PASSED WITH 100% OPTIMALITY & VALIDITY!")
    else:
        print("SOME CASES FAILED VERIFICATION.")
    print("==================================================")
    assert all_passed

if __name__ == "__main__":
    test_health_endpoint()
    test_malformed_request_handling()
    test_all_10_sample_cases()
