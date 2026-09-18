"""
FastAPI application — the single POST /optimize-energy endpoint
and GET /health.

Totals (total_grid_kwh, total_cost_bdt, peak_grid_kwh) are computed
exactly ONCE from the final hourly_plan in ``_compute_totals``.
"""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI

from app.llm_interpreter import interpret_notes
from app.optimizer import solve
from app.schemas import (
    HealthResponse,
    HourlyPlanEntry,
    HourInput,
    OptimizeRequest,
    OptimizeResponse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="GridWise LLM — BUP CSE FEST 2026",
    version="2.0",
    description="LLM-assisted 24-hour campus energy optimizer.",
)


# ── Utility: totals computed once ─────────────────────────────────────────

def _compute_totals(
    plan: list[HourlyPlanEntry], hours: list[HourInput]
) -> tuple[float, float, float]:
    """Return (total_grid_kwh, total_cost_bdt, peak_grid_kwh) from the final plan."""
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0
    for entry in plan:
        g = entry.grid_kwh
        total_grid += g
        total_cost += g * hours[entry.hour].tariff_bdt_per_kwh
        if g > peak_grid:
            peak_grid = g
    return round(total_grid, 2), round(total_cost, 2), round(peak_grid, 2)


def _build_summary(
    total_cost: float, total_grid: float, peak: float, n_directives: int
) -> str:
    return (
        f"Optimised 24-hour schedule: total grid import {total_grid} kWh, "
        f"cost {total_cost} BDT, peak hour grid {peak} kWh. "
        f"{n_directives} directive(s) applied."
    )


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/optimize-energy")
async def optimize_energy(request: OptimizeRequest) -> OptimizeResponse:
    t0 = time.perf_counter()

    # 1. Interpret operator notes (async, parallel, per-note isolation)
    directives = await interpret_notes(
        request.operator_notes, request.battery
    )

    # 2. Solve MILP
    hourly_plan = solve(request.hours, request.battery, directives)

    # 3. Compute totals exactly once from the final plan
    total_grid, total_cost, peak_grid = _compute_totals(
        hourly_plan, request.hours
    )

    n_applied = sum(1 for d in directives if d.applies)

    elapsed = time.perf_counter() - t0
    logger.info(
        "scenario=%s  cost=%.2f  grid=%.2f  peak=%.2f  directives=%d  time=%.3fs",
        request.scenario_id, total_cost, total_grid, peak_grid, n_applied, elapsed,
    )

    return OptimizeResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=directives,
        hourly_plan=hourly_plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
        plan_summary=_build_summary(total_cost, total_grid, peak_grid, n_applied),
    )
