from fastapi.testclient import TestClient
from ai_analyst.app import app
from typing import Any, Dict


def test_end_to_end_perceive_reason_act(monkeypatch):
    client = TestClient(app)

    # Fake planner: first propose webhook call, then finalize with summary
    decisions = [
        {
            "thought": "Send a critical notification to webhook before finalizing.",
            "action": "http_get",
            "input": {"url": "http://webhook.local/notify", "timeout": 1.0},
        },
        {
            "thought": "All data collected; respond with final summary.",
            "action": "final",
            "input": {"answer": "Critical alert: ssh brute force detected; SOC notified via webhook."},
        },
    ]

    async def fake_call_llm(prompt: str) -> str:
        # Return next decision as strict JSON string
        if decisions:
            import json
            d = decisions.pop(0)
            return json.dumps(d)
        # Default to final if extra calls happen
        return '{"thought":"done","action":"final","input":{"answer":"done"}}'

    # Monkeypatch planner LLM
    from ai_analyst import agent as agent_module
    monkeypatch.setattr(agent_module, "call_llm", fake_call_llm)

    # Monkeypatch webhook tool to avoid real HTTP
    from ai_analyst.tools import TOOLS_REGISTRY, ToolResult

    async def fake_http_get(url: str, headers: Dict[str, str] | None = None, timeout: float = 1.0):
        return ToolResult(True, {"status": 200, "text": "ok", "url": url})

    TOOLS_REGISTRY["http_get"]["func"] = fake_http_get  # type: ignore

    # 1) Create session (auto approval for immediate execution)
    r = client.post("/agent/session", json={"approval_mode": "auto"})
    assert r.status_code == 200
    session_id = r.json()["session"]["id"]

    # 2) Perceive: provide an alert-like message to seed context
    alert_text = "ALERT: ssh brute force from 1.2.3.4 on host srv-int (critical)"
    r = client.post(f"/agent/{session_id}/message", json={"content": alert_text})
    assert r.status_code == 200

    # 3) Reason+Act: run up to 3 steps; should call webhook then finalize
    r = client.post(f"/agent/{session_id}/run", json={"max_steps": 3})
    assert r.status_code == 200
    data: Dict[str, Any] = r.json()

    # Validate a tool execution occurred and final answer returned
    steps = data.get("steps", [])
    assert any(s.get("exec", {}).get("status") == "executed" for s in steps), "expected a tool execution"
    assert data.get("session", {}).get("done") is True

    # Final answer contains critical message suitable for webhook alerting
    final_msgs = [m for m in data.get("session", {}).get("messages", []) if m.get("role") == "assistant"]
    assert any("Critical alert" in (m.get("content") or "") for m in final_msgs)


