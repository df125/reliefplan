# ReliefPlan — MGH Anesthesia 5PM Coverage Planner

ReliefPlan builds the evening operating-room coverage plan: given the day's OR
schedule, the list of late-running rooms, and the PM staff roster, it assigns
attendings, CRNAs, and residents to every late room while respecting
supervision ratios, floor/building geography, fluoro restrictions, and
continuity preferences. See [docs/coverage-rules.md](docs/coverage-rules.md)
for the full rule set the solver encodes.

The web UI is paste-first: coordinators paste the Epic schedule and Qgenda
staff list, mark the late ORs, optionally describe last-minute changes in
plain English (parsed by an LLM), and generate the plan. The plan can then be
refined conversationally ("swap the attendings in OR 15 and OR 44").

## Setup

Requires Python 3.11+.

```bash
pip install -e ".[dev,web]"
```

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | For AI parsing only | Enables the LLM-powered paste parsing, situation updates, and plan refinement (Google Gemini). Without it, the deterministic local parsers still work and OR cards / staff rows can be edited manually; the AI endpoints return 503. |
| `GEMINI_MODEL` | No | Override the Gemini model (default `gemini-2.5-flash`). |
| `GEMINI_TIMEOUT` | No | Per-call LLM timeout in seconds (default 60; one retry on failure). |
| `RELIEFPLAN_DATA_DIR` | No | Directory for `roster.json` / `affinities.json` (default: repo root). Set for read-only or containerized installs. |

The key is read server-side only and never sent to the browser.

## Running

Web app (recommended):

```bash
uvicorn reliefplan.api:app --reload
# open http://127.0.0.1:8000
```

CLI:

```bash
reliefplan --sample          # run the built-in sample scenario
reliefplan plan input.json   # run on a JSON input file
```

## Tests and lint

```bash
pytest tests/ -v
ruff check reliefplan tests
```

CI runs both on every push and pull request (`.github/workflows/ci.yml`).

## Project layout

```
reliefplan/
├── models.py      # dataclasses: OperatingRoom, StaffMember, CoveragePlan…
├── zones.py       # OR → floor/building topology and compatibility rules
├── loader.py      # JSON → dataclass validation
├── algorithm.py   # the 7-step assignment solver
├── parser.py      # Gemini LLM parsers (schedule, staff, situation, refine)
├── refine.py      # applies structured LLM plan edits
├── api.py         # FastAPI endpoints + static mount
├── cli.py         # click CLI + sample fixture
└── static/
    └── index.html # single-page frontend
```

## Data files

`roster.json` and `affinities.json` accumulate staff rosters and preferences
across runs. They may contain real staff names and are gitignored — do not
commit them.
