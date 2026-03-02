import httpx
import time
from core.config_loader import get_apis
from core.rate_limiter import check_and_record

TOOL_DEFINITION = {
    "name": "agent_memory_tool",
    "description": """Use this tool to save and retrieve agent's long-term memory.

DO NOT call this tool for simple, isolated questions or basic commands (e.g., "ping my windows machine") that do not benefit from past context.

To RETRIEVE: action="retrieve_memory", params="describe the current task and relevant context"
To SAVE: action="save_memory", params="the fact to remember"

Save each individual fact as a SEPARATE call — never combine multiple facts into one.""",
    "examples": [
        "do you know my server address",
        "what did I tell you about my setup",
        "keep in mind I prefer dark mode",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": "Either 'retrieve_memory' or 'save_memory'",
            "enum": ["retrieve_memory", "save_memory"]
        },
        "params": {
            "type": "string",
            "description": "The query for retrieval, or the fact to save"
        }
    },
    "required": ["action", "params"]
}


def _cfg():
    return get_apis().get("memory", {})


async def _embed(text: str, model: str) -> list:
    await check_and_record("voyage_ai", wait=True)
    cfg = _cfg()
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {cfg['voyage_api_key']}"},
            json={"input": text, "model": model},
            timeout=30
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


async def _search(vector: list, limit: int = 1, threshold: float = 0.0) -> list:
    cfg = _cfg()
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{cfg['qdrant_url']}/collections/{cfg['qdrant_collection']}/points/search",
            json={"vector": vector, "limit": limit, "with_payload": True},
            timeout=10
        )
        r.raise_for_status()
        return [x for x in r.json().get("result", []) if x["score"] >= threshold]


async def execute(action: str, params: str) -> str:
    cfg = _cfg()
    qdrant_url = cfg["qdrant_url"]
    collection = cfg["qdrant_collection"]

    if action == "retrieve_memory":
        embedding = await _embed(params, cfg.get("voyage_retrieve_model", "voyage-4-lite"))
        results = await _search(embedding, limit=cfg.get("top_k", 10), threshold=cfg.get("score_threshold", 0.30))
        if not results:
            return "No relevant memories found."
        return "\n".join(
            f"- {r['payload']['text']} (relevance: {round(r['score'] * 100)}%)"
            for r in results
        )

    elif action == "save_memory":
        embedding = await _embed(params, cfg.get("voyage_save_model", "voyage-4-large"))
        existing = await _search(embedding, limit=1)

        if existing and existing[0]["score"] > cfg.get("dedup_threshold", 0.97):
            top = existing[0]
            async with httpx.AsyncClient() as client:
                await client.put(
                    f"{qdrant_url}/collections/{collection}/points",
                    json={"points": [{
                        "id": top["id"],
                        "vector": embedding,
                        "payload": {
                            "text": params,
                            "created_at": top["payload"].get("created_at"),
                            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        }
                    }]},
                    timeout=10
                )
            return "Memory updated."

        async with httpx.AsyncClient() as client:
            await client.put(
                f"{qdrant_url}/collections/{collection}/points",
                json={"points": [{
                    "id": int(time.time() * 1000),
                    "vector": embedding,
                    "payload": {
                        "text": params,
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    }
                }]},
                timeout=10
            )
        return "Memory saved."

    return "Invalid action. Use 'retrieve_memory' or 'save_memory'."
