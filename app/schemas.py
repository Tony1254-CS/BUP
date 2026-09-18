from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field

class HourEntry(BaseModel):
    hour: int = Field(..., ge=0, le=23, description="Hour of the day (0-23)")
    demand_kwh: float = Field(..., ge=0, description="Campus electricity demand in kWh")
    solar_kwh: float = Field(..., ge=0, description="Forecast rooftop solar generation in kWh")
    tariff_bdt_per_kwh: float = Field(..., ge=0, description="Grid electricity price for this hour in BDT")

class BatteryConfig(BaseModel):
    capacity_kwh: float = Field(..., gt=0, description="Maximum energy battery can store")
    initial_energy_kwh: float = Field(..., ge=0, description="Battery energy at start of hour 0")
    minimum_energy_kwh: float = Field(..., ge=0, description="Base reserve level battery must not go below")
    max_charge_kwh_per_hour: float = Field(..., ge=0, description="Maximum energy that can be added in one hour")
    max_discharge_kwh_per_hour: float = Field(..., ge=0, description="Maximum energy that can be discharged in one hour")

class OptimizeRequest(BaseModel):
    scenario_id: str
    operator_notes: List[str] = Field(..., min_length=1, max_length=3)
    hours: List[HourEntry] = Field(..., min_length=24, max_length=24)
    battery: BatteryConfig

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op"
]

class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[Dict[str, Any]] = None
    explanation: str

BatteryAction = Literal["charge", "discharge", "idle"]

class HourlyPlanEntry(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: BatteryAction
    battery_kwh: float
    battery_energy_after_kwh: float

class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str

class HealthResponse(BaseModel):
    status: str = "ok"
