import httpx
from auth.microsoft import get_access_token
from core.rate_limiter import check_and_record

TOOL_DEFINITION = {
    "name": "onedrive_tool",
    "description": "Manage Microsoft OneDrive files. Can list, search, upload, download, and delete files.",
    "examples": [
        "open the excel file",
        "save this to my microsoft storage",
        "find the word doc I was working on",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": "Action: 'list', 'search', 'upload', 'download', 'delete'",
            "enum": ["list", "search", "upload", "download", "delete"]
        },
        "data": {
            "type": "object",
            "description": "Action data. For search: {query}. For upload: {name, content}. For download/delete: {file_id}."
        }
    },
    "required": ["action"]
}

GRAPH_URL = "https://graph.microsoft.com/v1.0"


async def execute(action: str, data: dict = None) -> tuple:
    await check_and_record("microsoft", wait=True)
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    data = data or {}

    async with httpx.AsyncClient() as client:

        if action == "list":
            r = await client.get(f"{GRAPH_URL}/me/drive/root/children", headers=headers, timeout=30)
            r.raise_for_status()
            items = r.json().get("value", [])
            output = [f"- {i['name']} | ID: {i['id']} | {i.get('size', 0)} bytes" for i in items]
            return "\n".join(output) if output else "No files.", None, None

        elif action == "search":
            r = await client.get(f"{GRAPH_URL}/me/drive/search(q='{data['query']}')", headers=headers, timeout=30)
            r.raise_for_status()
            items = r.json().get("value", [])
            output = [f"- {i['name']} | ID: {i['id']}" for i in items[:10]]
            return "\n".join(output) if output else "No results.", None, None

        elif action == "download":
            meta = await client.get(f"{GRAPH_URL}/me/drive/items/{data['file_id']}", headers=headers, timeout=30)
            meta.raise_for_status()
            filename = meta.json()["name"]
            dl = await client.get(f"{GRAPH_URL}/me/drive/items/{data['file_id']}/content",
                                  headers=headers, follow_redirects=True, timeout=60)
            dl.raise_for_status()
            return f"Downloaded {filename}", dl.content, filename

        elif action == "upload":
            content = data["content"].encode() if isinstance(data["content"], str) else data["content"]
            r = await client.put(f"{GRAPH_URL}/me/drive/root:/{data['name']}:/content",
                                 headers={**headers, "Content-Type": "application/octet-stream"},
                                 content=content, timeout=60)
            r.raise_for_status()
            return f"Uploaded: {data['name']}", None, None

        elif action == "delete":
            r = await client.delete(f"{GRAPH_URL}/me/drive/items/{data['file_id']}", headers=headers, timeout=30)
            r.raise_for_status()
            return "File deleted.", None, None

    return f"Unknown action: {action}", None, None
