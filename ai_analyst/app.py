from typing import Any, Dict, List, Optional
import os
import time
import orjson
import httpx
from elasticsearch import Elasticsearch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from .agent import (
    create_session,
    add_user_message,
    step as agent_step,
    run as agent_run,
    get_session as agent_get_session,
    approve_pending,
)
from .tools import TOOLS_REGISTRY
from .core import env, make_es, call_llm
import threading
import asyncio


def make_consumer():
    try:
        from confluent_kafka import Consumer  # type: ignore
    except Exception as e:
        raise RuntimeError("Kafka consumer unavailable: confluent-kafka not installed") from e
    conf = {
        "bootstrap.servers": env("KAFKA_BOOTSTRAP", "kafka:29092"),
        "group.id": env("KAFKA_GROUP_ID", "ai-analyst"),
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
    }
    return Consumer(conf)



def build_prompt(alert: Dict[str, Any]) -> str:
    title = f"Alert from {alert.get('source','unknown')}"
    original = alert.get("source_event_json") or alert.get("raw_text") or alert.get("event", {}).get("original")
    return (
        f"{title}\n"
        f"Time: {alert.get('@timestamp')}\n"
        f"Anomaly: {alert.get('anomaly_score')}\n"
        f"Template: {alert.get('template')} (id={alert.get('template_id')})\n"
        f"Original:\n{original}\n"
        "Summarize what happened and suggest next investigative steps."
    )


def run_consumer_loop() -> None:
    topic = env("KAFKA_TOPIC", "syslog-alerts")
    out_index = env("OUTPUT_INDEX", "alerts-enriched")

    consumer = make_consumer()
    es = make_es()
    consumer.subscribe([topic])
    print(f"[ai-analyst] consuming from topic={topic}, indexing to {out_index}")

    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                # Import here to avoid hard dependency at import time
                from confluent_kafka import KafkaException  # type: ignore
                raise KafkaException(msg.error())

            try:
                alert = orjson.loads(msg.value())
            except Exception:
                continue

            prompt = build_prompt(alert)
            try:
                summary = asyncio.run(call_llm(prompt))
            except Exception as e:
                summary = f"LLM unavailable. Heuristic summary: {prompt[-200:]} ({e})"

            doc = {
                "@timestamp": alert.get("@timestamp"),
                "source": alert.get("source"),
                "summary": summary,
                "original_alert": alert,
            }
            try:
                es.index(index=out_index, document=doc)
            except Exception as e:
                print(f"[ai-analyst] index error: {e}")
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        consumer.close()


class AnalyzeRequest(BaseModel):
    query: str
    index: Optional[str] = None
    time_range: Optional[str] = None


def _default_query(query: str, time_range: Optional[str]) -> Dict[str, Any]:
    must: List[Dict[str, Any]] = [
        {"multi_match": {"query": query, "fields": [
            "message^2", "event.original^2", "source_event_json", "raw_text", "host.name", "process.name"
        ]}}
    ]
    if time_range:
        must.append({"range": {"@timestamp": {"gte": f"now-{time_range}"}}})
    return {"bool": {"must": must}}


def translate_to_es_dsl(query: str, time_range: Optional[str]) -> Dict[str, Any]:
    prompt = (
        "You translate a natural-language SOC question into an Elasticsearch 8 DSL JSON.\n"
        "Return ONLY valid JSON with a top-level 'query' field and optional 'sort' and 'size'.\n"
        "Use a time range filter on '@timestamp' if provided. Fields include 'message', 'event.original', 'source_event_json', 'host.name', 'process.name'.\n"
        f"NL query: {query}\n"
        f"Time range: {time_range or 'none'}\n"
    )
    try:
        text = asyncio.run(call_llm(prompt))
        data = orjson.loads(text)
        if not isinstance(data, dict) or "query" not in data:
            raise ValueError("missing query")
        return data
    except Exception:
        return {"query": _default_query(query, time_range), "sort": [{"@timestamp": "desc"}], "size": 50}


def format_insights(hits: List[Dict[str, Any]], original_query: str) -> str:
    if not hits:
        return "No matching events found. Consider broadening the time range or keywords."
    try:
        compact = []
        for h in hits[:20]:
            src = h.get("_source") or {}
            compact.append({
                "@ts": src.get("@timestamp"),
                "host": src.get("host", {}).get("name") if isinstance(src.get("host"), dict) else src.get("host"),
                "proc": src.get("process", {}).get("name") if isinstance(src.get("process"), dict) else src.get("process"),
                "msg": src.get("message") or src.get("event", {}).get("original") or src.get("raw_text")
            })
        prompt = (
            "You are a SOC analyst. Summarize patterns and provide 2-3 concise, actionable next steps.\n"
            f"Question: {original_query}\n"
            f"Sample events: {orjson.dumps(compact).decode()}\n"
        )
        return asyncio.run(call_llm(prompt))
    except Exception:
        by_host: Dict[str, int] = {}
        for h in hits:
            src = h.get("_source") or {}
            host = src.get("host", {}).get("name") if isinstance(src.get("host"), dict) else src.get("host")
            if host:
                by_host[host] = by_host.get(host, 0) + 1
        top = sorted(by_host.items(), key=lambda x: x[1], reverse=True)[:3]
        parts = [f"Matches: {len(hits)}"]
        if top:
            parts.append("Top hosts: " + ", ".join([f"{h}({c})" for h, c in top]))
        parts.append("Next: refine keywords, review top hosts, pivot by process.")
        return "; ".join(parts)


app = FastAPI(title="Search Analysis Agent")


@app.post("/analyze")
def analyze(req: AnalyzeRequest) -> dict:
    es = make_es()
    index = req.index or env("SEARCH_INDEX", "alerts-enriched")
    dsl = translate_to_es_dsl(req.query, req.time_range)
    body = {
        "query": dsl.get("query", _default_query(req.query, req.time_range)),
        "sort": dsl.get("sort", [{"@timestamp": "desc"}]),
        "size": int(dsl.get("size", 50)),
    }
    allow_partial = (env("ANALYZE_ALLOW_PARTIAL", "1") or "").lower() not in ("0", "false", "no")
    try:
        res = es.search(index=index, body=body)
        hits = res.get("hits", {}).get("hits", [])
        total = res.get("hits", {}).get("total", {}).get("value", len(hits))
    except Exception as e:
        if not allow_partial:
            raise HTTPException(status_code=500, detail=f"Elasticsearch error: {e}")
        # Offline/partial mode: return empty results but keep 200 contract
        hits = []
        total = 0
    insights = format_insights(hits, req.query)
    samples = []
    for h in hits[:5]:
        src = h.get("_source") or {}
        samples.append({
            "@timestamp": src.get("@timestamp"),
            "host": src.get("host", {}).get("name") if isinstance(src.get("host"), dict) else src.get("host"),
            "message": src.get("message") or src.get("event", {}).get("original") or src.get("raw_text"),
        })
    return {"dsl": body, "total": total, "insights": insights, "samples": samples}


# --- Agent endpoints ---


class CreateSessionRequest(BaseModel):
    approval_mode: Optional[str] = None  # "auto" or "manual"


@app.post("/agent/session")
def agent_session_create(req: CreateSessionRequest) -> dict:
    s = create_session(req.approval_mode)
    return {"session": s.to_dict()}


class PostMessageRequest(BaseModel):
    content: str


@app.post("/agent/{session_id}/message")
def agent_post_message(session_id: str, req: PostMessageRequest) -> dict:
    s = add_user_message(session_id, req.content)
    return {"session": s.to_dict()}


@app.get("/agent/tools")
def agent_list_tools() -> dict:
    tools = {k: {"description": v.get("description"), "schema": v.get("schema")} for k, v in TOOLS_REGISTRY.items()}
    return {"tools": tools}


@app.get("/agent/{session_id}")
def agent_get_state(session_id: str) -> dict:
    s = agent_get_session(session_id)
    return {"session": s.to_dict()}


@app.post("/agent/{session_id}/step")
def agent_step_once(session_id: str) -> dict:
    try:
        return agent_step(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


class RunRequest(BaseModel):
    max_steps: Optional[int] = 5


@app.post("/agent/{session_id}/run")
def agent_run_loop(session_id: str, req: RunRequest) -> dict:
    try:
        return agent_run(session_id, max_steps=int(req.max_steps or 5))
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


@app.post("/agent/{session_id}/approve")
def agent_approve(session_id: str) -> dict:
    try:
        return approve_pending(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")


def main() -> None:
    run_mode = (env("RUN_MODE", "api") or "api").lower()
    if run_mode == "consumer":
        # Run only the Kafka consumer loop (no API)
        run_consumer_loop()
        return
    if run_mode == "both":
        # Start consumer in background and serve API
        t = threading.Thread(target=run_consumer_loop, name="alerts-consumer", daemon=True)
        t.start()
    # Default: API only
    try:
        import uvicorn
        host = env("API_HOST", "0.0.0.0")
        port = int(env("API_PORT", "8080"))
        uvicorn.run(app, host=host, port=port, log_level="info")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()



