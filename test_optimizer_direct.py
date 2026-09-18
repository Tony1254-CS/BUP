import json
from app.schemas import HourEntry, BatteryConfig, DirectiveInterpretation
from app.optimizer import solve_energy_schedule

def run_tests():
    with open('sample_cases.json', 'r') as f:
        pack = json.load(f)

    print(f"Loaded {len(pack['cases'])} cases.")
    all_passed = True
    for case in pack['cases']:
        cid = case['id']
        hours = [HourEntry(**h) for h in case['input']['hours']]
        battery = BatteryConfig(**case['input']['battery'])
        expected_dirs = [DirectiveInterpretation(**d) for d in case['expected_output']['directive_interpretation']]

        plan, tot_grid, tot_cost, peak_grid = solve_energy_schedule(hours, battery, expected_dirs)
        exp_cost = case['expected_output']['total_cost_bdt']
        exp_grid = case['expected_output']['total_grid_kwh']

        cost_diff = abs(tot_cost - exp_cost)
        grid_diff = abs(tot_grid - exp_grid)
        status = 'PASS' if cost_diff <= 0.05 else 'FAIL'
        if status == 'FAIL':
            all_passed = False
        print(f"[{status}] {cid}: Team Cost={tot_cost} Exp Cost={exp_cost} Diff={cost_diff:.2f} | Team Grid={tot_grid} Exp Grid={exp_grid}")

    print("\nAll cases passed mathematically!" if all_passed else "\nSome differences found.")

if __name__ == '__main__':
    run_tests()
