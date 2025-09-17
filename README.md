# AI Analyst Agent

Agentic SOC assistant: perceives (collects context), reasons (plans multi-step actions), and reacts (executes tools) over your SIEM data.

## Run (API)
```bash
docker compose up -d --build api
curl -s http://localhost:8080/analyze -X POST -H 'Content-Type: application/json' \
  -d '{"query":"ssh brute force","time_range":"24h"}' | jq .
```

## Local tests
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r ai_analyst/requirements.txt pytest
ANALYZE_ALLOW_PARTIAL=1 python -m pytest -q tests/test_agent.py
```

## Structure
- `ai_analyst/app.py`: FastAPI API + RUN_MODE switch (api/consumer/both)
- `ai_analyst/requirements.txt`: Python dependencies
- `tests/test_agent.py`: Contract and integration-style tests
