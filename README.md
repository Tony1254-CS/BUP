# GridWise LLM — BUP CSE FEST 2026

LLM-assisted 24-hour campus energy optimizer for the BUP CSE FEST 2026 Hackathon Online Preliminary.

## Architecture

```
POST /optimize-energy
  │
  ├─ 1. LLM Interpreter (Gemini 2.5 Flash)
  │     • One async call PER operator note (never batched)
  │     • 8s timeout per call, asyncio.gather for parallelism
  │     • Guardrail validation (raises on invalid, falls back to no_op)
  │
  ├─ 2. MILP Optimizer (SciPy HiGHS)
  │     • Binary is_charge[h] + is_discharge[h] with mutual exclusion
  │     • 168 variables (7 per hour × 24 hours)
  │     • All constraints traceable to official spec
  │
  └─ 3. Totals Computation
        • Computed exactly once from the final hourly_plan
```

## Quick Start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Set API key
echo GEMINI_API_KEY=your_key_here > .env

# 3. Run
uvicorn app.main:app --port 8000

# 4. Test
pytest tests/ -v
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Google Gemini API key |
| `SKIP_LLM` | No | Set `true` to skip LLM calls (all notes → no_op) |

## API Endpoints

### `GET /health`
Returns `{"status": "ok"}`.

### `POST /optimize-energy`
Accepts the full input payload (scenario_id, operator_notes, hours, battery).
Returns the optimized schedule with directive interpretations.

## Testing

```bash
# All tests (guardrails + optimizer + e2e)
pytest tests/ -v

# Optimizer only (no LLM)
pytest tests/test_optimizer.py -v

# Paraphrase tests (requires GEMINI_API_KEY)
pytest tests/test_paraphrase.py -v -s
```

## Docker

```bash
docker build -t gridwise .
docker run -p 8000:8000 -e GEMINI_API_KEY=your_key gridwise
```
