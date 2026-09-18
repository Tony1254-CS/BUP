"""
LLM-based operator-note interpreter.

Architecture:
  - One async Gemini call PER operator note — never batched.
  - Each call has retry with exponential backoff for 429/503 errors.
  - If a single call ultimately fails, that note falls back to no_op.
  - SKIP_LLM=true → every note becomes no_op (for optimizer-only testing).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from app.guardrails import DirectiveValidationError, validate_directive
from app.schemas import BatteryInput, DirectiveInterpretation

logger = logging.getLogger(__name__)

PER_CALL_TIMEOUT = 60.0  # seconds total per note (including retries)
MAX_RETRIES = 3
BASE_DELAY = 5.0  # seconds between retries

SYSTEM_PROMPT = """\
You are a campus energy scheduling assistant.  You will receive ONE
operator note and battery parameters.  Your task: decide whether the
note contains a scheduling directive and, if so, classify it.

RULES
  1  Six directive_type values are valid:
     solar_reduction, minimum_battery_reserve, no_charge_window,
     no_discharge_window, max_grid_window, no_op
  2  Time windows are START-INCLUSIVE, END-EXCLUSIVE.
     "from 1 PM to 3 PM" -> hours [13, 14].
     "overnight from 10 PM to 6 AM" -> hours [22, 23, 0, 1, 2, 3, 4, 5].
  3  "Reduce solar by 80%" -> factor = 0.20 (remaining fraction, not the
     percentage removed).
     "Reduce solar by X%" -> factor = (100-X)/100.
     "Solar output at 30%" -> factor = 0.30.
  4  For minimum_battery_reserve, give the absolute energy floor in kWh.
     "Keep battery above 40%" with capacity C -> minimum_energy_kwh = 0.4 * C.
  5  Chatty, irrelevant, or ambiguous notes -> no_op with applies=false.
  6  Do NOT invent constraints not present in the note.
  7  hours must be a sorted list of integers in ascending order, each 0..23.
     For overnight windows that wrap around midnight, list the hours in
     chronological order starting from the PM hours.

OUTPUT — return ONLY a JSON object:
{
  "applies": true/false,
  "directive_type": "...",
  "structured_adjustment": { ... } or null,
  "explanation": "one-sentence reason"
}
"""


def _make_no_op(note_index: int, reason: str = "Defaulted to no_op.") -> dict[str, Any]:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": reason,
    }


def _is_retryable(exc: Exception) -> bool:
    """Check if the exception is a retryable API error (429/503)."""
    msg = str(exc)
    return "429" in msg or "503" in msg or "RESOURCE_EXHAUSTED" in msg or "UNAVAILABLE" in msg


async def _call_gemini_with_retry(
    note: str, note_index: int, battery: BatteryInput
) -> dict[str, Any]:
    """
    Call Gemini with retry + exponential backoff for 429/503 errors.
    """
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY", "")
    client = genai.Client(api_key=api_key)

    user_prompt = (
        f"Battery parameters:\n"
        f"  capacity_kwh = {battery.capacity_kwh}\n"
        f"  initial_energy_kwh = {battery.initial_energy_kwh}\n"
        f"  minimum_energy_kwh = {battery.minimum_energy_kwh}\n"
        f"  max_charge_kwh_per_hour = {battery.max_charge_kwh_per_hour}\n"
        f"  max_discharge_kwh_per_hour = {battery.max_discharge_kwh_per_hour}\n\n"
        f'Operator note (index {note_index}):\n"{note}"'
    )

    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.aio.models.generate_content(
                model="gemini-3.6-flash",
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.0,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
            text = response.text
            if text is None:
                raise ValueError("Gemini returned empty text")
            return json.loads(text)

        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES and _is_retryable(exc):
                # Parse retry delay from error if available
                delay = BASE_DELAY * (2 ** attempt)
                retry_match = re.search(r"retryDelay.*?(\d+)", str(exc))
                if retry_match:
                    delay = max(delay, int(retry_match.group(1)) + 1)
                logger.info(
                    "Note %d: Retryable error (attempt %d/%d), waiting %.0fs: %s",
                    note_index, attempt + 1, MAX_RETRIES + 1, delay, type(exc).__name__
                )
                await asyncio.sleep(delay)
            else:
                raise

    raise last_exc  # type: ignore


async def _interpret_single(
    note: str, note_index: int, battery: BatteryInput
) -> dict[str, Any]:
    """
    Interpret one note with timeout + retry + guardrail validation.
    On ANY final failure -> no_op for this note only.
    """
    try:
        raw = await asyncio.wait_for(
            _call_gemini_with_retry(note, note_index, battery),
            timeout=PER_CALL_TIMEOUT,
        )
        raw["note_index"] = note_index
        validate_directive(raw, battery)
        return raw

    except asyncio.TimeoutError:
        logger.warning("Note %d: Timed out after %.0fs", note_index, PER_CALL_TIMEOUT)
        return _make_no_op(note_index, "LLM call timed out; defaulted to no_op.")

    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Note %d: Parse error: %s", note_index, exc)
        return _make_no_op(note_index, f"Parse error: {exc}")

    except DirectiveValidationError as exc:
        logger.warning("Note %d: Guardrail rejection: %s", note_index, exc)
        return _make_no_op(note_index, f"Guardrail rejection: {exc}")

    except Exception as exc:
        logger.warning("Note %d: Failed after retries: %s", note_index, exc)
        return _make_no_op(note_index, f"Error: {exc}")


async def interpret_notes(
    notes: list[str], battery: BatteryInput
) -> list[DirectiveInterpretation]:
    """
    Interpret all operator notes and return validated directives.

    - SKIP_LLM=true -> all notes become no_op (for optimizer testing).
    - Otherwise: one async Gemini call per note with staggered starts
      to avoid rate limit bursts.
    """
    skip = os.getenv("SKIP_LLM", "").strip().lower() in ("true", "1", "yes")

    if skip or not os.getenv("GEMINI_API_KEY", ""):
        logger.info("LLM skipped (SKIP_LLM=%s, key=%s)",
                     os.getenv("SKIP_LLM", ""), "set" if os.getenv("GEMINI_API_KEY") else "unset")
        raw_list = [_make_no_op(i, "LLM skipped.") for i in range(len(notes))]
    else:
        # Stagger calls by 1s to reduce rate-limit pressure
        results = []
        for i, note in enumerate(notes):
            if i > 0:
                await asyncio.sleep(1.0)
            results.append(_interpret_single(note, i, battery))
        raw_list = await asyncio.gather(*results)

    return [DirectiveInterpretation(**d) for d in raw_list]
