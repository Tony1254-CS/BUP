import logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from app.schemas import (
    OptimizeRequest,
    OptimizeResponse,
    HealthResponse
)
from app.interpreter import interpret_operator_notes
from app.optimizer import solve_energy_schedule

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("gridwise.main")

app = FastAPI(
    title="GridWise Campus Energy Optimizer API",
    description="LLM-assisted operator directive interpretation and optimal 24-hour energy scheduling for BUP CSE Fest 2026 Hackathon",
    version="2.0.0"
)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handles malformed requests and returns controlled 400 Bad Request."""
    logger.warning(f"Request validation error: {exc.errors()}")
    return JSONResponse(
        status_code=400,
        content={"detail": "Malformed JSON or structurally invalid request.", "errors": str(exc.errors())}
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Safely captures unexpected exceptions to avoid leaking secrets or stack traces."""
    logger.error(f"Internal server error: {exc}", exc_info=False)
    return JSONResponse(
        status_code=500,
        content={"detail": "Controlled internal error processing energy optimization."}
    )

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Readiness and liveness probe for the judging harness.
    Returns HTTP 200 with status: ok.
    """
    return HealthResponse(status="ok")

@app.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(request: OptimizeRequest):
    """
    Primary challenge endpoint:
    1. Interprets operator notes via LLM / generative model into structured directives.
    2. Deterministically validates guardrails (ranges, bounds, ascending hour ordering).
    3. Solves the 24-hour campus energy scheduling Linear Program (HiGHS).
    4. Recalculates total cost, grid import, and peak load.
    5. Returns compliant response matching canonical specification.
    """
    try:
        # Step 1: Interpret operator notes into machine-checkable directives
        directives = interpret_operator_notes(request.operator_notes, request.battery)

        # Step 2: Solve 24-hour energy scheduling optimization problem
        hourly_plan, total_grid_kwh, total_cost_bdt, peak_grid_kwh = solve_energy_schedule(
            request.hours,
            request.battery,
            directives
        )

        # Step 3: Summarize plan strategy
        applied_types = [d.directive_type for d in directives if d.applies]
        if applied_types:
            plan_summary = f"Applied directives ({', '.join(applied_types)}), satisfied all battery bounds and end-of-day neutrality, and minimized total grid electricity cost."
        else:
            plan_summary = "All notes processed as no_op; optimized battery charging and discharging to minimize grid cost while ensuring end-of-day neutrality."

        return OptimizeResponse(
            scenario_id=request.scenario_id,
            directive_interpretation=directives,
            hourly_plan=hourly_plan,
            total_grid_kwh=total_grid_kwh,
            total_cost_bdt=total_cost_bdt,
            peak_grid_kwh=peak_grid_kwh,
            plan_summary=plan_summary
        )

    except Exception as exc:
        logger.error(f"Error during energy optimization: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Failed to optimize energy schedule under given constraints."
        )
