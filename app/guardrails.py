import math
from typing import List, Dict, Any, Optional
from app.schemas import DirectiveInterpretation, BatteryConfig, DirectiveType

VALID_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op"
}

def sanitize_hours(raw_hours: Any) -> List[int]:
    """Ensures hours are unique integers in [0..23], sorted ascending."""
    if not isinstance(raw_hours, (list, tuple)):
        return []
    valid_hours = set()
    for h in raw_hours:
        try:
            h_int = int(round(float(h)))
            if 0 <= h_int <= 23:
                valid_hours.add(h_int)
        except (ValueError, TypeError):
            continue
    return sorted(list(valid_hours))

def validate_and_sanitize_directive(
    raw_directive: Dict[str, Any],
    expected_index: int,
    battery: BatteryConfig
) -> DirectiveInterpretation:
    """
    Validates and sanitizes a single directive interpretation against
    Section 08 LLM Interpretation Guardrails.
    """
    directive_type: str = str(raw_directive.get("directive_type", "no_op")).strip().lower()
    if directive_type not in VALID_DIRECTIVE_TYPES:
        directive_type = "no_op"

    raw_applies = raw_directive.get("applies", False)
    explanation = str(raw_directive.get("explanation", "")).strip() or "Standard directive processing."
    raw_adjustment = raw_directive.get("structured_adjustment")

    if directive_type == "no_op" or not raw_applies:
        return DirectiveInterpretation(
            note_index=expected_index,
            applies=False,
            directive_type="no_op",
            structured_adjustment=None,
            explanation=explanation or "This note does not affect today's 24-hour energy schedule."
        )

    # For all other directives: applies must be True and adjustment must match required schema
    sanitized_adjustment: Dict[str, Any] = {}
    if not isinstance(raw_adjustment, dict):
        # Fallback to no_op if adjustment missing for an active directive
        return DirectiveInterpretation(
            note_index=expected_index,
            applies=False,
            directive_type="no_op",
            structured_adjustment=None,
            explanation="Malformed adjustment data; treated as no_op safely."
        )

    hours = sanitize_hours(raw_adjustment.get("hours", []))
    if not hours:
        # Without hours, directive cannot be applied
        return DirectiveInterpretation(
            note_index=expected_index,
            applies=False,
            directive_type="no_op",
            structured_adjustment=None,
            explanation="No valid hours specified; treated as no_op safely."
        )

    sanitized_adjustment["hours"] = hours

    if directive_type == "solar_reduction":
        factor = raw_adjustment.get("factor")
        try:
            factor_val = float(factor)
            if math.isnan(factor_val) or math.isinf(factor_val):
                factor_val = 1.0
            factor_val = max(0.0, min(1.0, factor_val))
        except (ValueError, TypeError):
            factor_val = 1.0
        sanitized_adjustment["factor"] = factor_val

    elif directive_type == "minimum_battery_reserve":
        min_reserve = raw_adjustment.get("minimum_energy_kwh")
        try:
            res_val = float(min_reserve)
            if math.isnan(res_val) or math.isinf(res_val):
                res_val = battery.minimum_energy_kwh
            res_val = max(0.0, min(battery.capacity_kwh, res_val))
        except (ValueError, TypeError):
            res_val = battery.minimum_energy_kwh
        sanitized_adjustment["minimum_energy_kwh"] = res_val

    elif directive_type == "max_grid_window":
        max_grid = raw_adjustment.get("max_grid_kwh")
        try:
            grid_val = float(max_grid)
            if math.isnan(grid_val) or math.isinf(grid_val):
                grid_val = 1e6
            grid_val = max(0.0, grid_val)
        except (ValueError, TypeError):
            grid_val = 1e6
        sanitized_adjustment["max_grid_kwh"] = grid_val

    elif directive_type in ("no_charge_window", "no_discharge_window"):
        # only hours required
        pass

    return DirectiveInterpretation(
        note_index=expected_index,
        applies=True,
        directive_type=directive_type,  # type: ignore
        structured_adjustment=sanitized_adjustment,
        explanation=explanation
    )

def guardrail_directives(
    raw_directives: List[Dict[str, Any]],
    num_notes: int,
    battery: BatteryConfig
) -> List[DirectiveInterpretation]:
    """
    Enforces that exactly num_notes interpretations exist, sorted by note_index 0..N-1,
    with no missing or duplicate mappings.
    """
    # Map by note_index if available
    indexed: Dict[int, Dict[str, Any]] = {}
    for item in raw_directives:
        if isinstance(item, dict):
            idx = item.get("note_index")
            if idx is not None and isinstance(idx, int) and 0 <= idx < num_notes:
                indexed[idx] = item

    results: List[DirectiveInterpretation] = []
    for i in range(num_notes):
        raw_item = indexed.get(i)
        if raw_item is None:
            # Check if positional item exists
            if i < len(raw_directives) and isinstance(raw_directives[i], dict):
                raw_item = raw_directives[i]
            else:
                raw_item = {
                    "note_index": i,
                    "applies": False,
                    "directive_type": "no_op",
                    "structured_adjustment": None,
                    "explanation": "No directive extracted; defaulted to no_op."
                }
        sanitized = validate_and_sanitize_directive(raw_item, i, battery)
        results.append(sanitized)

    return results
