import httpx
import time
from core.config_loader import get_apis
from core.rate_limiter import check_and_record

TOOL_DEFINITION = {
    "name": "agent_memory_tool",
    "description": """Use this tool to save, retrieve, list, and delete the agent's long-term memory.

DO NOT call this tool for simple, isolated questions or basic commands (e.g., "ping my windows machine") that do not benefit from past context.

To RETRIEVE: action="retrieve_memory", params="describe the current task and relevant context"
To SAVE: action="save_memory", params="the fact to remember"
To LIST ALL: action="list_memories", params="" — shows everything currently stored
To DELETE: action="delete_memory", params="<memory_id>" — removes a specific memory by its ID (get IDs from list_memories)

Save each individual fact as a SEPARATE call — never combine multiple facts into one.""",
    "examples": [
        "do you know my server address",
        "what did I tell you about my setup",
        "keep in mind I prefer dark mode",
        "show me everything you remember about me",
        "forget that I said my server is at 192.168.1.1",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": "Action: 'retrieve_memory', 'save_memory', 'list_memories', 'delete_memory'",
            "enum": ["retrieve_memory", "save_memory", "list_memories", "delete_memory"]
        },
        "params": {
            "type": "string",
            "description": (
                "For retrieve_memory: describe what you're looking for. "
                "For save_memory: the fact to store. "
                "For list_memories: pass empty string or a keyword to filter by. "
                "For delete_memory: the numeric ID of the memory to delete (from list_memories)."
            )
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

    # Retrieve memory
    if action == "retrieve_memory":
        embedding = await _embed(params, cfg.get("voyage_retrieve_model", "voyage-4-lite"))
        results = await _search(
            embedding,
            limit=cfg.get("top_k", 10),
            threshold=cfg.get("score_threshold", 0.30)
        )
        if not results:
            return "No relevant memories found."
        return "\n".join(
            f"- [{r['id']}] {r['payload']['text']} (relevance: {round(r['score'] * 100)}%)"
            for r in results
        )

    # Save memory
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
            return f"Memory updated (ID: {top['id']})."

        new_id = int(time.time() * 1000)
        async with httpx.AsyncClient() as client:
            await client.put(
                f"{qdrant_url}/collections/{collection}/points",
                json={"points": [{
                    "id": new_id,
                    "vector": embedding,
                    "payload": {
                        "text": params,
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    }
                }]},
                timeout=10
            )
        return f"Memory saved (ID: {new_id})."

    # List all memories
    elif action == "list_memories":
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{qdrant_url}/collections/{collection}/points/scroll",
                json={
                    "limit": 100,
                    "with_payload": True,
                    "with_vector": False,
                },
                timeout=15
            )
            r.raise_for_status()
            points = r.json().get("result", {}).get("points", [])

        if not points:
            return "No memories stored yet."

        keyword = params.strip().lower() if params.strip() else None
        if keyword:
            points = [
                p for p in points
                if keyword in p["payload"].get("text", "").lower()
            ]
            if not points:
                return f"No memories found matching '{keyword}'."

        lines = [f"Total memories: {len(points)}\n"]
        for p in points:
            created = p["payload"].get("created_at", "?")
            updated = p["payload"].get("updated_at", "")
            timestamp = f"updated {updated}" if updated else f"created {created}"
            lines.append(f"[{p['id']}] {p['payload'].get('text', '?')} ({timestamp})")
        return "\n".join(lines)

    # Delete a specific memory by ID
    elif action == "delete_memory":
        try:
            memory_id = int(params.strip())
        except ValueError:
            return (
                "delete_memory requires a numeric ID. "
                "Use list_memories to find the ID of the memory you want to remove."
            )
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{qdrant_url}/collections/{collection}/points/delete",
                json={"points": [memory_id]},
                timeout=10
            )
            r.raise_for_status()
        return f"Memory {memory_id} deleted."

    return "Invalid action. Use 'retrieve_memory', 'save_memory', 'list_memories', or 'delete_memory'."
