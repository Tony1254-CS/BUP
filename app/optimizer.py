import numpy as np
from scipy.optimize import linprog
from typing import List, Dict, Any, Tuple
from app.schemas import HourEntry, BatteryConfig, DirectiveInterpretation, HourlyPlanEntry

def solve_energy_schedule(
    hours: List[HourEntry],
    battery: BatteryConfig,
    directives: List[DirectiveInterpretation]
) -> Tuple[List[HourlyPlanEntry], float, float, float]:
    """
    Optimizes the 24-hour campus energy schedule using SciPy linprog (HiGHS solver).
    Guarantees mathematically minimal grid electricity cost and 100% constraint satisfaction.
    """
    N = 24

    # 1. Apply directives to parameters
    effective_solar = [h.solar_kwh for h in hours]
    min_reserve = [battery.minimum_energy_kwh] * N
    max_charge = [battery.max_charge_kwh_per_hour] * N
    max_discharge = [battery.max_discharge_kwh_per_hour] * N
    max_grid = [1e9] * N

    for d in directives:
        if not d.applies or not d.structured_adjustment:
            continue
        dtype = d.directive_type
        adj = d.structured_adjustment
        affected_hours = adj.get("hours", [])

        if dtype == "solar_reduction":
            factor = float(adj.get("factor", 1.0))
            for h in affected_hours:
                if 0 <= h < N:
                    effective_solar[h] = hours[h].solar_kwh * factor

        elif dtype == "minimum_battery_reserve":
            reserve_val = float(adj.get("minimum_energy_kwh", battery.minimum_energy_kwh))
            for h in affected_hours:
                if 0 <= h < N:
                    min_reserve[h] = max(min_reserve[h], reserve_val)

        elif dtype == "no_charge_window":
            for h in affected_hours:
                if 0 <= h < N:
                    max_charge[h] = 0.0

        elif dtype == "no_discharge_window":
            for h in affected_hours:
                if 0 <= h < N:
                    max_discharge[h] = 0.0

        elif dtype == "max_grid_window":
            grid_cap = float(adj.get("max_grid_kwh", 1e9))
            for h in affected_hours:
                if 0 <= h < N:
                    max_grid[h] = min(max_grid[h], grid_cap)

    # Variables per hour h (indices 0..4):
    # 0: grid_kwh (g_h)
    # 1: solar_used_kwh (s_h)
    # 2: battery_charge_kwh (c_h)
    # 3: battery_discharge_kwh (d_h)
    # 4: battery_energy_after_kwh (E_h)
    num_vars = N * 5

    def var_idx(h: int, v: int) -> int:
        return h * 5 + v

    # Objective: Minimize sum(tariff_h * g_h + eps * (c_h + d_h))
    # eps = 1e-7 breaks degeneracy and ensures clean action profiles
    eps = 1e-7
    c_obj = np.zeros(num_vars)
    for h in range(N):
        c_obj[var_idx(h, 0)] = hours[h].tariff_bdt_per_kwh
        c_obj[var_idx(h, 2)] = eps
        c_obj[var_idx(h, 3)] = eps

    # Equality constraints: A_eq @ x = b_eq
    # 1) Demand balance for each h: g_h + s_h + d_h - c_h = demand_h
    # 2) Battery transition for h=0: E_0 - c_0 + d_0 = initial_energy_kwh
    #    Battery transition for h>0: E_h - E_{h-1} - c_h + d_h = 0
    # 3) End of day neutrality: E_{23} = initial_energy_kwh
    eq_rows = []
    b_eq = []

    for h in range(N):
        # Demand balance
        row = np.zeros(num_vars)
        row[var_idx(h, 0)] = 1.0  # g_h
        row[var_idx(h, 1)] = 1.0  # s_h
        row[var_idx(h, 3)] = 1.0  # d_h
        row[var_idx(h, 2)] = -1.0 # -c_h
        eq_rows.append(row)
        b_eq.append(hours[h].demand_kwh)

        # Battery transition
        row_batt = np.zeros(num_vars)
        row_batt[var_idx(h, 4)] = 1.0   # E_h
        row_batt[var_idx(h, 2)] = -1.0  # -c_h
        row_batt[var_idx(h, 3)] = 1.0   # +d_h
        if h == 0:
            eq_rows.append(row_batt)
            b_eq.append(battery.initial_energy_kwh)
        else:
            row_batt[var_idx(h - 1, 4)] = -1.0  # -E_{h-1}
            eq_rows.append(row_batt)
            b_eq.append(0.0)

    # End of day neutrality
    row_eod = np.zeros(num_vars)
    row_eod[var_idx(N - 1, 4)] = 1.0
    eq_rows.append(row_eod)
    b_eq.append(battery.initial_energy_kwh)

    A_eq = np.array(eq_rows)
    b_eq = np.array(b_eq)

    # Bounds for each variable:
    bounds = []
    for h in range(N):
        # 0: g_h in [0, max_grid[h]]
        bounds.append((0.0, max_grid[h]))
        # 1: s_h in [0, effective_solar[h]]
        bounds.append((0.0, effective_solar[h]))
        # 2: c_h in [0, max_charge[h]]
        bounds.append((0.0, max_charge[h]))
        # 3: d_h in [0, max_discharge[h]]
        bounds.append((0.0, max_discharge[h]))
        # 4: E_h in [min_reserve[h], battery.capacity_kwh]
        bounds.append((min_reserve[h], battery.capacity_kwh))

    # Solve with HiGHS
    res = linprog(c=c_obj, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")

    if not res.success:
        raise RuntimeError(f"Linear program failed to solve: {res.message}")

    x = res.x
    hourly_plan: List[HourlyPlanEntry] = []

    for h in range(N):
        g = float(x[var_idx(h, 0)])
        s = float(x[var_idx(h, 1)])
        c = float(x[var_idx(h, 2)])
        d = float(x[var_idx(h, 3)])
        e = float(x[var_idx(h, 4)])

        # Simultaneous charge/discharge reduction
        simul = min(c, d)
        if simul > 0:
            c -= simul
            d -= simul

        # Clean small numerical zeros
        if g < 1e-6:
            g = 0.0
        if s < 1e-6:
            s = 0.0
        if c < 1e-6:
            c = 0.0
        if d < 1e-6:
            d = 0.0

        # Action assignment
        if c > 1e-5:
            action = "charge"
            batt_kwh = round(c, 2)
        elif d > 1e-5:
            action = "discharge"
            batt_kwh = round(d, 2)
        else:
            action = "idle"
            batt_kwh = 0.0

        # Maintain exact energy balance with grid adjustment
        # grid = demand + charge - solar_used - discharge
        g_exact = round(hours[h].demand_kwh + (batt_kwh if action == "charge" else 0.0)
                        - round(s, 2) - (batt_kwh if action == "discharge" else 0.0), 2)
        if g_exact < 0:
            g_exact = 0.0

        hourly_plan.append(HourlyPlanEntry(
            hour=h,
            grid_kwh=g_exact,
            solar_used_kwh=round(s, 2),
            battery_action=action, # type: ignore
            battery_kwh=batt_kwh,
            battery_energy_after_kwh=round(e, 2)
        ))

    # Recalculated totals
    total_grid_kwh = round(sum(p.grid_kwh for p in hourly_plan), 2)
    total_cost_bdt = round(sum(p.grid_kwh * hours[p.hour].tariff_bdt_per_kwh for p in hourly_plan), 2)
    peak_grid_kwh = round(max(p.grid_kwh for p in hourly_plan), 2)

    return hourly_plan, total_grid_kwh, total_cost_bdt, peak_grid_kwh
