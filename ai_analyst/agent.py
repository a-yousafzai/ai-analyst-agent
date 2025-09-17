from typing import Any, Dict, List, Optional, Tuple
import threading
import time
import orjson
import uuid
from .core import env, call_llm
from .tools import TOOLS_REGISTRY


class AgentSession:
    def __init__(self, approval_mode: str = "auto"):
        self.id: str = str(uuid.uuid4())
        self.approval_mode: str = approval_mode if approval_mode in ("auto", "manual") else "auto"
        self.messages: List[Dict[str, Any]] = []  # role: user|assistant|tool, name optional
        self.pending_action: Optional[Dict[str, Any]] = None
        self.created_at: float = time.time()
        self.updated_at: float = self.created_at
        self.done: bool = False
        self.last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "approval_mode": self.approval_mode,
            "messages": self.messages[-30:],
            "pending_action": self.pending_action,
            "done": self.done,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_SESSIONS: Dict[str, AgentSession] = {}
_LOCK = threading.Lock()


def create_session(approval_mode: Optional[str] = None) -> AgentSession:
    mode = (approval_mode or env("AGENT_APPROVAL_MODE", "auto") or "auto").lower()
    s = AgentSession(approval_mode=mode)
    with _LOCK:
        _SESSIONS[s.id] = s
    return s


def get_session(session_id: str) -> AgentSession:
    with _LOCK:
        s = _SESSIONS.get(session_id)
    if not s:
        raise KeyError("session not found")
    return s


def add_user_message(session_id: str, content: str) -> AgentSession:
    s = get_session(session_id)
    s.messages.append({"role": "user", "content": content})
    s.updated_at = time.time()
    return s


def _tool_instructions() -> str:
    parts: List[str] = [
        "You are an autonomous SOC assistant. Think step-by-step. Use tools to fetch data, then decide next actions.",
        "When you are ready to provide the final answer, use action 'final' with an 'answer' field.",
        "Always respond in strict JSON with keys: thought, action, input.",
        "Available tools:",
    ]
    for name, meta in TOOLS_REGISTRY.items():
        parts.append(f"- {name}: {meta.get('description')} schema={meta.get('schema')}")
    parts.append("Example: {\"thought\":\"...\", \"action\":\"es_search\", \"input\":{\"index\":\"alerts-enriched\", \"query\":{...}}}")
    parts.append("Final answer example: {\"thought\":\"...\", \"action\":\"final\", \"input\":{\"answer\":\"...\"}}")
    return "\n".join(parts)


def _build_dialogue(messages: List[Dict[str, Any]], limit: int = 10) -> str:
    recent = messages[-limit:]
    compact: List[Dict[str, Any]] = []
    for m in recent:
        compact.append({k: v for k, v in m.items() if k in ("role", "name", "content")})
    return orjson.dumps(compact).decode()


def _parse_action(text: str) -> Tuple[str, Dict[str, Any], str]:
    raw = text.strip()
    try:
        data = orjson.loads(raw)
    except Exception:
        # try to extract json between backticks
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                data = orjson.loads(raw[start : end + 1])
            except Exception:
                return "final", {"answer": raw[:500]}, "non_json"
        else:
            return "final", {"answer": raw[:500]}, "non_json"
    action = str(data.get("action", "final")).strip()
    action_input = data.get("input") or {}
    if not isinstance(action_input, dict):
        action_input = {"value": action_input}
    thought = str(data.get("thought", "")).strip()
    return action, action_input, thought


def plan_next_action(session: AgentSession) -> Dict[str, Any]:
    prompt = (
        _tool_instructions()
        + "\n"
        + "Dialogue (JSON array):\n"
        + _build_dialogue(session.messages)
        + "\nRespond with strict JSON only."
    )
    try:
        text = call_llm.__wrapped__(prompt) if hasattr(call_llm, "__wrapped__") else None  # type: ignore
    except Exception:
        text = None
    if text is None:
        try:
            # call_llm is async; follow app.py style
            import asyncio
            text = asyncio.run(call_llm(prompt))
        except Exception as e:
            session.last_error = f"llm_error: {e}"
            return {"action": "final", "input": {"answer": "LLM unavailable. Provide concise heuristic next steps."}, "thought": "fallback"}
    action, action_input, thought = _parse_action(text)
    return {"action": action, "input": action_input, "thought": thought}


def execute_action(session: AgentSession, action: str, action_input: Dict[str, Any]) -> Dict[str, Any]:
    session.updated_at = time.time()
    if action == "final":
        answer = str(action_input.get("answer") or "")
        session.messages.append({"role": "assistant", "content": answer})
        session.done = True
        return {"status": "final", "answer": answer}

    meta = TOOLS_REGISTRY.get(action)
    if not meta:
        err = f"unknown_tool: {action}"
        session.last_error = err
        session.messages.append({"role": "assistant", "content": err})
        return {"status": "error", "error": err}

    # Approval gate
    if session.approval_mode == "manual":
        session.pending_action = {"tool": action, "input": action_input}
        return {"status": "awaiting_approval", "pending_action": session.pending_action}

    # Auto-execute
    try:
        import asyncio
        func = meta["func"]
        result = asyncio.run(func(**action_input))  # type: ignore
        out = result.to_dict()  # type: ignore
    except Exception as e:
        out = {"ok": False, "error": str(e)}
    session.messages.append({"role": "tool", "name": action, "content": out})
    return {"status": "executed", "result": out}


def approve_pending(session_id: str) -> Dict[str, Any]:
    s = get_session(session_id)
    if not s.pending_action:
        return {"status": "no_pending"}
    action = s.pending_action.get("tool")
    action_input = s.pending_action.get("input") or {}
    s.pending_action = None
    s.approval_mode = s.approval_mode  # no-op
    return execute_action(s, action, action_input)


def step(session_id: str) -> Dict[str, Any]:
    s = get_session(session_id)
    if s.done:
        return {"status": "done", "session": s.to_dict()}
    # If awaiting approval, do nothing until approved
    if s.pending_action:
        return {"status": "awaiting_approval", "pending_action": s.pending_action, "session": s.to_dict()}
    decision = plan_next_action(s)
    thought = decision.get("thought")
    if thought:
        s.messages.append({"role": "assistant", "content": f"Thought: {thought}"})
    return {"decision": decision, "exec": execute_action(s, decision["action"], decision.get("input") or {}), "session": s.to_dict()}


def run(session_id: str, max_steps: int = 5) -> Dict[str, Any]:
    s = get_session(session_id)
    steps: List[Dict[str, Any]] = []
    for _ in range(max_steps):
        res = step(session_id)
        steps.append(res)
        if res.get("status") in ("done",):
            break
        if res.get("exec", {}).get("status") == "awaiting_approval":
            break
        if s.done:
            break
    return {"steps": steps, "session": s.to_dict()}


