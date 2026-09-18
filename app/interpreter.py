import os
import re
import json
import logging
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from app.schemas import BatteryConfig, DirectiveInterpretation
from app.guardrails import guardrail_directives

load_dotenv()
logger = logging.getLogger("gridwise.interpreter")

# System instruction for Gemini
SYSTEM_PROMPT = """You are an expert energy scheduling assistant for the BUP Smart Campus (GridWise).
Your task is to interpret 1 to 3 campus operator natural-language notes and convert them into structured directives.

There are exactly 6 supported directive types:
1. `solar_reduction`: Reduce usable solar during specific hours.
   structured_adjustment: {"hours": [int], "factor": float}
   NOTE: `factor` is the usable fraction remaining (between 0.0 and 1.0).
   - "80% reduction" means factor is 0.2 (20% remains).
   - "usable solar roughly 25%" means factor is 0.25.
   - "leave about half" means factor is 0.5.
   - "one-fifth of normal solar" means factor is 0.2.

2. `minimum_battery_reserve`: Keep battery energy at or above a required level.
   structured_adjustment: {"hours": [int], "minimum_energy_kwh": float}
   - If stated as absolute kWh (e.g. "at least 90 kWh"), use that number.
   - If stated as a percentage of capacity (e.g. "at least 50% of the battery capacity"), compute capacity * percentage.

3. `no_charge_window`: Battery charging is unavailable during specific hours.
   structured_adjustment: {"hours": [int]}

4. `no_discharge_window`: Battery discharging is unavailable during specific hours.
   structured_adjustment: {"hours": [int]}

5. `max_grid_window`: Grid import may not exceed a stated amount during specific hours.
   structured_adjustment: {"hours": [int], "max_grid_kwh": float}

6. `no_op`: The note does not affect today's 24-hour energy schedule (e.g. cafeteria menus, sports events, library hours, seminar bookings).
   applies: false
   structured_adjustment: null

TIME CONVENTIONS:
- All hours use whole-hour intervals, start-inclusive and end-exclusive.
- "noon until 2 PM" -> [12, 13]
- "2 AM until 5 AM" -> [2, 3, 4]
- "6 PM until 9 PM" -> [18, 19, 20]
- "6 PM until 10 PM" -> [18, 19, 20, 21]
- "7 PM until 9 PM" -> [19, 20]
- "7 PM until 10 PM" -> [19, 20, 21]
- "11 AM until 1 PM" -> [11, 12]
- "11 AM until 2 PM" or "between 11 AM and 2 PM" -> [11, 12, 13]
- "10 AM until noon" -> [10, 11]
- "2 PM until 4 PM" -> [14, 15]
- "5 PM until 7 PM" -> [17, 18]
- "6 PM until 8 PM" -> [18, 19]
- "13:00 to 15:00" -> [13, 14]
- The hours array must be sorted in strictly ascending order of unique integers in 0..23.

RULES:
- Return a JSON object with a key "directives" containing a list of directive objects.
- Each directive object MUST have:
  - "note_index": int (0, 1, ... N-1 corresponding to each input note)
  - "applies": bool (true for relevant directives, false for no_op)
  - "directive_type": string (one of the 6 allowed types)
  - "structured_adjustment": object or null
  - "explanation": string (short human-readable reason)
"""

def parse_time_window(text: str) -> List[int]:
    """Helper to extract whole-hour start-inclusive, end-exclusive hours from natural text."""
    lower = text.lower()
    
    # Patterns like "from 6 PM until 10 PM", "between 11 AM and 2 PM", "noon until 2 PM"
    def word_to_hour(w: str, period: Optional[str] = None) -> Optional[int]:
        w = w.strip().lower()
        if w in ("noon", "12 noon", "12 pm"):
            return 12
        if w in ("midnight", "12 am"):
            return 0
        m = re.match(r"^(\d{1,2})(?::00)?\s*(am|pm)?$", w)
        if m:
            val = int(m.group(1))
            meridiem = m.group(2) or period
            if meridiem == "pm" and val < 12:
                val += 12
            elif meridiem == "am" and val == 12:
                val = 0
            return val
        return None

    # Try matching common range patterns
    range_patterns = [
        r"(?:from|between)\s+(noon|midnight|\d{1,2}(?::00)?\s*(?:am|pm)?)\s+(?:until|to|and)\s+(noon|midnight|\d{1,2}(?::00)?\s*(?:am|pm)?)",
        r"(\d{1,2})(?::00)?\s*(am|pm)?\s*(?:-|to|until)\s*(\d{1,2})(?::00)?\s*(am|pm)",
        r"(\d{1,2}):00\s*(?:-|to|until|and)\s*(\d{1,2}):00"
    ]

    for pat in range_patterns:
        match = re.search(pat, lower)
        if match:
            groups = match.groups()
            if len(groups) == 2:
                # E.g. "noon", "2 pm" or "13:00", "15:00"
                start_h = word_to_hour(groups[0])
                end_h = word_to_hour(groups[1])
                # If start doesn't specify AM/PM but end does, inherit
                if start_h is not None and end_h is not None:
                    if "pm" in groups[1] and start_h < 12 and "am" not in groups[0] and start_h <= end_h % 12:
                        if end_h >= 12:
                            start_h += 12
                    if 0 <= start_h < end_h <= 24:
                        return list(range(start_h, end_h))
            elif len(groups) == 4:
                # E.g. "1", "pm", "3", "pm" or "1", None, "3", "pm"
                s_val, s_p, e_val, e_p = groups
                end_p = e_p.lower() if e_p else None
                start_p = s_p.lower() if s_p else end_p
                start_h = int(s_val)
                end_h = int(e_val)
                if start_p == "pm" and start_h < 12:
                    start_h += 12
                elif start_p == "am" and start_h == 12:
                    start_h = 0
                if end_p == "pm" and end_h < 12:
                    end_h += 12
                elif end_p == "am" and end_h == 12:
                    end_h = 0
                if 0 <= start_h < end_h <= 24:
                    return list(range(start_h, end_h))

    return []

def fallback_interpret_note(
    note: str,
    note_index: int,
    battery: BatteryConfig
) -> Dict[str, Any]:
    """
    Deterministic rule-based fallback interpreter for offline operation
    or transient API unavailability.
    """
    lower = note.lower()
    
    # 1. Distractor detection
    distractor_keywords = [
        "cafeteria", "sports", "registration", "library", "book-return",
        "club notices", "seminar room", "lunch", "meeting", "holiday",
        "weather report", "parking", "lost and found"
    ]
    if any(dk in lower for dk in distractor_keywords) and not any(k in lower for k in ["solar", "battery", "grid", "charge", "discharge", "kw"]):
        return {
            "note_index": note_index,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "This note does not affect today's 24-hour energy schedule."
        }

    hours = parse_time_window(note)

    # 2. Solar Reduction
    if any(k in lower for k in ["solar", "pv", "panel", "cloud cover"]):
        factor = 1.0
        # Check percentage reduction: "80% reduction" -> factor 0.2
        red_pct = re.search(r"(\d{1,3})%\s*(?:reduction|drop)", lower)
        if red_pct:
            factor = round(1.0 - (float(red_pct.group(1)) / 100.0), 3)
        else:
            # Check remaining percentage: "roughly 25%", "to about 20%", "half", "one-fifth"
            rem_pct = re.search(r"(?:about|to|roughly|remain|leave|leaves)\s*(\d{1,3})%", lower)
            if rem_pct:
                factor = round(float(rem_pct.group(1)) / 100.0, 3)
            elif "half" in lower:
                factor = 0.5
            elif "one-fifth" in lower or "one fifth" in lower:
                factor = 0.2
            elif "one-quarter" in lower or "one fourth" in lower:
                factor = 0.25

        if hours:
            return {
                "note_index": note_index,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": hours, "factor": factor},
                "explanation": f"Solar output reduced to {factor*100:.0f}% during the specified window."
            }

    # 3. Minimum Battery Reserve
    if any(k in lower for k in ["reserve", "remain in the battery", "stored in the battery", "minimum energy", "keep at least"]):
        min_kwh = battery.minimum_energy_kwh
        # Check if percentage of battery capacity: "50% of the battery capacity"
        pct_match = re.search(r"(\d{1,3})%\s*(?:of the battery capacity|capacity)", lower)
        if pct_match:
            pct_val = float(pct_match.group(1)) / 100.0
            min_kwh = round(battery.capacity_kwh * pct_val, 2)
        else:
            # Check kWh number: "at least 120 kWh"
            kwh_match = re.search(r"(\d+(?:\.\d+)?)\s*kwh", lower)
            if kwh_match:
                min_kwh = float(kwh_match.group(1))

        if hours:
            return {
                "note_index": note_index,
                "applies": True,
                "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {"hours": hours, "minimum_energy_kwh": min_kwh},
                "explanation": f"Keep at least {min_kwh} kWh in battery reserve during stated window."
            }

    # 4. No Charge Window
    if any(k in lower for k in ["not charge", "charging is disabled", "charging is unavailable", "charger will be isolated", "charging circuit"]):
        if hours:
            return {
                "note_index": note_index,
                "applies": True,
                "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": hours},
                "explanation": "Battery charging is disabled during this period."
            }

    # 5. No Discharge Window
    if any(k in lower for k in ["not discharge", "discharge is disabled", "discharge is unavailable", "do not discharge"]):
        if hours:
            return {
                "note_index": note_index,
                "applies": True,
                "directive_type": "no_discharge_window",
                "structured_adjustment": {"hours": hours},
                "explanation": "Battery discharging is disabled during this period."
            }

    # 6. Max Grid Window
    if any(k in lower for k in ["grid import", "grid intake", "feeder", "transformer limit", "substation"]):
        max_kwh = 1e9
        kwh_match = re.search(r"(?:exceed|limit is|below)\s*(\d+(?:\.\d+)?)\s*kwh", lower)
        if not kwh_match:
            kwh_match = re.search(r"(\d+(?:\.\d+)?)\s*kwh", lower)
        if kwh_match:
            max_kwh = float(kwh_match.group(1))

        if hours:
            return {
                "note_index": note_index,
                "applies": True,
                "directive_type": "max_grid_window",
                "structured_adjustment": {"hours": hours, "max_grid_kwh": max_kwh},
                "explanation": f"Grid import is capped at {max_kwh} kWh during this period."
            }

    # Default to no_op
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "This note does not affect today's 24-hour energy schedule."
    }

def interpret_with_gemini(
    operator_notes: List[str],
    battery: BatteryConfig,
    api_key: str,
    model_name: str = "gemini-2.5-flash"
) -> Optional[List[Dict[str, Any]]]:
    """
    Calls Google Gemini using the official google-genai SDK with structured JSON output.
    """
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        prompt = f"""Campus Battery Configuration:
- Capacity: {battery.capacity_kwh} kWh
- Initial Energy: {battery.initial_energy_kwh} kWh
- Base Minimum Reserve: {battery.minimum_energy_kwh} kWh
- Max Charge Rate: {battery.max_charge_kwh_per_hour} kWh/h
- Max Discharge Rate: {battery.max_discharge_kwh_per_hour} kWh/h

Campus Operator Notes:
"""
        for i, note in enumerate(operator_notes):
            prompt += f"[Note {i}]: {note}\n"

        prompt += "\nOutput the JSON array of directive interpretations for each note in note_index order (0 to N-1)."

        # Define schema or JSON response
        candidate_models = [model_name, "gemini-2.0-flash", "gemini-1.5-flash"]
        for mdl in candidate_models:
            try:
                response = client.models.generate_content(
                    model=mdl,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        temperature=0.0
                    )
                )
                text = response.text.strip()
                data = json.loads(text)
                if isinstance(data, dict) and "directives" in data:
                    return data["directives"]
                elif isinstance(data, list):
                    return data
                elif isinstance(data, dict):
                    # Might be a single object or wrapped
                    for v in data.values():
                        if isinstance(v, list):
                            return v
            except Exception as e:
                logger.warning(f"Failed with model {mdl}: {e}")
                continue

    except Exception as exc:
        logger.error(f"Gemini API invocation error: {exc}")
    return None

def interpret_operator_notes(
    operator_notes: List[str],
    battery: BatteryConfig
) -> List[DirectiveInterpretation]:
    """
    Main LLM interpretation entrypoint with deterministic validation and fallback safety.
    1. Attempts LLM interpretation via Gemini Flash if GEMINI_API_KEY is available.
    2. Gracefully falls back to deterministic rule interpreter if key is absent or API fails.
    3. Runs deterministic guardrail validator to ensure 100% compliant structured output.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    raw_directives = None

    if api_key:
        raw_directives = interpret_with_gemini(operator_notes, battery, api_key)

    if not raw_directives:
        # Resilient fallback parser
        raw_directives = [
            fallback_interpret_note(note, i, battery)
            for i, note in enumerate(operator_notes)
        ]

    # Guardrails guarantee exact ordering, hours sorting, and bounds
    return guardrail_directives(raw_directives, len(operator_notes), battery)
