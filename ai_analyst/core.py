from typing import Optional
import os
import httpx
from elasticsearch import Elasticsearch


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


def make_es() -> Elasticsearch:
    return Elasticsearch(env("ELASTICSEARCH_URL", "http://elasticsearch:9200"))


async def call_llm(prompt: str) -> str:
    api_key = env("OPENAI_API_KEY")
    base_url = env("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = env("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        return (prompt[-300:]).strip()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a SOC analyst. Write concise, actionable summaries."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 200,
    }
    async with httpx.AsyncClient(timeout=20.0, base_url=base_url) as client:
        r = await client.post("/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()


