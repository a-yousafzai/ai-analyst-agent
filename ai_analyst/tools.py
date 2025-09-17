from typing import Any, Dict, Optional, List
import asyncio
import time
import httpx
from .core import make_es, env


class ToolResult:
    def __init__(self, ok: bool, output: Any, error: Optional[str] = None, usage: Optional[Dict[str, Any]] = None):
        self.ok = ok
        self.output = output
        self.error = error
        self.usage = usage or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "output": self.output, "error": self.error, "usage": self.usage}


async def tool_es_search(index: str, query: Dict[str, Any], size: int = 50) -> ToolResult:
    try:
        es = make_es()
        body = {"query": query, "size": size, "sort": [{"@timestamp": "desc"}]}
        res = es.search(index=index, body=body)
        hits: List[Dict[str, Any]] = res.get("hits", {}).get("hits", [])
        return ToolResult(True, {"total": len(hits), "hits": hits[: size]})
    except Exception as e:
        return ToolResult(False, None, str(e))


async def tool_http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 15.0) -> ToolResult:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers)
            return ToolResult(True, {"status": r.status_code, "headers": dict(r.headers), "text": r.text[:20000]})
    except Exception as e:
        return ToolResult(False, None, str(e))


async def tool_sleep(seconds: float) -> ToolResult:
    try:
        await asyncio.sleep(seconds)
        return ToolResult(True, {"slept": seconds})
    except Exception as e:
        return ToolResult(False, None, str(e))


TOOLS_REGISTRY = {
    "es_search": {
        "description": "Search Elasticsearch index with a provided DSL query.",
        "schema": {"index": "str", "query": "dict", "size": "int?"},
        "func": tool_es_search,
    },
    "http_get": {
        "description": "HTTP GET request for enrichment (e.g., threat intel).",
        "schema": {"url": "str", "headers": "dict?", "timeout": "float?"},
        "func": tool_http_get,
    },
    "sleep": {
        "description": "Sleep for N seconds to wait for data or rate limits.",
        "schema": {"seconds": "float"},
        "func": tool_sleep,
    },
}


