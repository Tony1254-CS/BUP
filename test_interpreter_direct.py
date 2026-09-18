import json
from app.schemas import BatteryConfig
from app.interpreter import interpret_operator_notes

def test_all_sample_notes():
    with open('sample_cases.json', 'r') as f:
        pack = json.load(f)

    print(f"Testing Interpreter across {len(pack['cases'])} cases...")
    all_matched = True
    for case in pack['cases']:
        cid = case['id']
        notes = case['input']['operator_notes']
        battery = BatteryConfig(**case['input']['battery'])
        expected_dirs = case['expected_output']['directive_interpretation']

        team_dirs = interpret_operator_notes(notes, battery)

        print(f"\n--- {cid} ({len(notes)} notes) ---")
        for i, (t_d, e_d) in enumerate(zip(team_dirs, expected_dirs)):
            match_type = (t_d.directive_type == e_d['directive_type'])
            match_applies = (t_d.applies == e_d['applies'])
            match_adj = (t_d.structured_adjustment == e_d['structured_adjustment'])

            case_ok = match_type and match_applies and match_adj
            if not case_ok:
                all_matched = False
                print(f"  Note {i}: FAIL")
                print(f"    Team: {t_d}")
                print(f"    Exp : {e_d}")
            else:
                print(f"  Note {i}: PASS [{t_d.directive_type} | hours={t_d.structured_adjustment.get('hours') if t_d.structured_adjustment else None}]")

    print("\n==========================================")
    if all_matched:
        print("ALL 10 SAMPLE CASES DIRECTIVE INTERPRETATIONS MATCH 100%!")
    else:
        print("SOME DIRECTIVE INTERPRETATIONS DIFFER.")
    print("==========================================")

if __name__ == '__main__':
    test_all_sample_notes()
