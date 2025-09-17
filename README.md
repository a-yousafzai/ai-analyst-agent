# AI Analyst Agent

Agentic SOC assistant: perceives (ingests alert context and fetches more data), reasons (multi-step planning with an LLM policy), and acts (executes tool actions with optional human approval) over your SIEM data.

## What it does (Perceive → Reason → Act)

- Perceive:
  - Kafka consumer ingests alerts and writes enriched summaries to Elasticsearch (`RUN_MODE=consumer|both`).
  - Agent tools fetch investigation data: `es_search` (ES DSL), `http_get` (enrichment), `sleep` (wait backoff).
  - Session memory stores dialogue and tool outputs for policy context.

- Reason:
  - A compact LLM policy plans the next step (`plan_next_action`) given the session history and tool catalog, emitting strict JSON instructions.

- Act:
  - Executor runs the requested tool immediately (auto) or waits for explicit human approval (manual). Supports single-step and looped runs.

## Quickstart (Docker Compose)

```bash
docker compose up -d --build api

# Verify API is up (host maps 8081 → container 8080)
curl -s http://localhost:8081/agent/tools | jq .

# Existing summarize endpoint still works
curl -s http://localhost:8081/analyze -X POST -H 'Content-Type: application/json' \
  -d '{"query":"ssh brute force","time_range":"24h"}' | jq .
```

Note: Compose may warn that `version` is obsolete; it can be removed without affecting functionality.

## Agent API

- POST `/agent/session`
  - Create a session; optional `approval_mode: "auto" | "manual"` (default derives from `AGENT_APPROVAL_MODE`).
  - Body: `{ "approval_mode": "manual" }`
  - Returns: `{ session: { id, approval_mode, messages, pending_action, done, ... } }`

- POST `/agent/{session_id}/message`
  - Append a user goal/instruction.
  - Body: `{ "content": "Investigate ssh brute force in last 24h" }`

- GET `/agent/{session_id}`
  - Fetch session state/memory.

- GET `/agent/tools`
  - List available tools with basic schemas.

- POST `/agent/{session_id}/step`
  - Run one reasoning step; may immediately execute a tool (auto) or return `awaiting_approval` (manual).

- POST `/agent/{session_id}/run`
  - Body: `{ "max_steps": 5 }` (optional)
  - Run multiple steps until final answer, approval needed, or step limit reached.

- POST `/agent/{session_id}/approve`
  - In manual mode, approve and execute the currently pending tool action.

### Example flow (manual approval)

```bash
# 1) Create session
SESSION=$(curl -sX POST localhost:8081/agent/session \
  -H 'content-type: application/json' -d '{"approval_mode":"manual"}' | jq -r .session.id)

# 2) Add a goal
curl -sX POST localhost:8081/agent/$SESSION/message \
  -H 'content-type: application/json' -d '{"content":"Investigate ssh brute force in last 24h"}' | jq .

# 3) Step once (likely proposes an es_search)
curl -sX POST localhost:8081/agent/$SESSION/step | jq .

# 4) Approve the pending action (if awaiting approval)
curl -sX POST localhost:8081/agent/$SESSION/approve | jq .

# 5) Optionally run a short loop
curl -sX POST localhost:8081/agent/$SESSION/run -H 'content-type: application/json' \
  -d '{"max_steps":3}' | jq .
```

## System architecture

- `ai_analyst/app.py`: FastAPI app, `/analyze` endpoint, agent endpoints, and `RUN_MODE` for Kafka consumer.
- `ai_analyst/agent.py`: Agent sessions, memory, LLM planner, executor, approval gate, step/run orchestration.
- `ai_analyst/tools.py`: Tool implementations and `TOOLS_REGISTRY`.
- `ai_analyst/core.py`: Shared `env`, `make_es`, `call_llm` to avoid circular imports.
- `tests/test_agent.py`: Contract tests for `/analyze` and ES integration.

## Environment variables

- API / runtime
  - `RUN_MODE`: `api` (default), `consumer`, or `both`.
  - `API_HOST`, `API_PORT`: container bind host/port (compose maps host 8081 → container 8080).
  - `AGENT_APPROVAL_MODE`: default session mode (`auto` or `manual`).

- Elasticsearch
  - `ELASTICSEARCH_URL`: e.g., `http://localhost:9200`.
  - `SEARCH_INDEX`: default index for `/analyze` (default `alerts-enriched`).
  - `OUTPUT_INDEX`: index for enriched consumer writes (default `alerts-enriched`).

- LLM
  - `OPENAI_API_KEY`, `OPENAI_BASE_URL` (default `https://api.openai.com/v1`), `OPENAI_MODEL` (default `gpt-4o-mini`).
  - If `OPENAI_API_KEY` is absent, the system returns a heuristic fallback using the prompt tail.

- Analyze fallback
  - `ANALYZE_ALLOW_PARTIAL`: if truthy (default), `/analyze` returns 200 with empty results when ES is unavailable.

- Kafka consumer
  - `KAFKA_BOOTSTRAP` (default `kafka:29092`), `KAFKA_GROUP_ID` (default `ai-analyst`), `KAFKA_TOPIC` (default `syslog-alerts`).

## Docker

- `ai_analyst/Dockerfile` builds and runs `python -m ai_analyst.app`.
- `docker-compose.yml` builds the API and exposes `8081` on the host.

Start/stop:
```bash
docker compose up -d --build api
docker compose logs -f api
docker compose stop api
```

## Local development & tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r ai_analyst/requirements.txt pytest

# Contract tests (Elasticsearch optional; falls back to partial mode for most tests)
ANALYZE_ALLOW_PARTIAL=1 python -m pytest -q tests/test_agent.py
```

## Notes & safety

- Current tools are read-only (ES search, HTTP GET, sleep). Enabling write/destructive tools is supported by the approval gate; use `approval_mode: manual` in production.
- Keep sessions short to minimize context length; the planner compacts recent history automatically.

