import io
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from auth.google import get_credentials
from core.rate_limiter import check_and_record

TOOL_DEFINITION = {
    "name": "google_drive_tool",
    "description": (
        "Manage Google Drive files and folders. Supports: list, search, upload, download, "
        "copy, move, rename, create_folder, delete (trash), delete_permanent, get_share_link, share."
    ),
    "examples": [
        "save this to my drive",
        "find the report I uploaded last week",
        "what folders do I have in my drive",
        "copy the file to the Reports folder",
        "move this file to Archives",
        "rename the file to Q4_Report",
        "get a shareable link for this file",
        "share this file with john@example.com",
        "permanently delete this file",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": (
                "Action to perform: "
                "'list' - list files; "
                "'search' - search by Drive query; "
                "'upload' - upload a file; "
                "'download' - download a file; "
                "'copy' - copy a file; "
                "'move' - move file to another folder; "
                "'rename' - rename a file or folder; "
                "'create_folder' - create a new folder; "
                "'delete' - move file to trash; "
                "'delete_permanent' - permanently delete a file; "
                "'get_share_link' - get a shareable link; "
                "'share' - share with a specific user."
            ),
            "enum": [
                "list", "search", "upload", "download",
                "copy", "move", "rename",
                "create_folder", "delete", "delete_permanent",
                "get_share_link", "share"
            ]
        },
        "data": {
            "type": "object",
            "description": (
                "Action-specific data: "
                "list: {query?} raw Drive query (default: trashed=false). "
                "search: {query} raw Drive query. ALWAYS use 'name contains' not 'name =' for name searches, and NEVER include file extensions (e.g. use 'NEMA Church Program' not 'NEMA Church Program.xlsx'). Example: \"name contains 'report' and trashed=false\". "
                "upload: {name, content, mime_type?, parent_id?}. "
                "download: {file_id}. "
                "copy: {file_id, name?, parent_id?}. "
                "move: {file_id, new_parent_id}. "
                "rename: {file_id, name}. "
                "create_folder: {name, parent_id?}. "
                "delete: {file_id}. "
                "delete_permanent: {file_id}. "
                "get_share_link: {file_id, anyone_can_view?} - set anyone_can_view=true to make public. "
                "share: {file_id, email, role?} - role: reader/writer/commenter (default: reader)."
            )
        }
    },
    "required": ["action"]
}


def _service():
    return build("drive", "v3", credentials=get_credentials())


async def execute(action: str, data: dict = None) -> tuple:
    """Returns (text_result, file_bytes, filename)"""
    await check_and_record("google_apis", wait=True)
    svc = _service()
    data = data or {}

    if action == "list":
        q = data.get("query", "trashed=false")
        res = svc.files().list(
            pageSize=20,
            fields="files(id, name, mimeType, size, modifiedTime)",
            q=q
        ).execute()
        files = res.get("files", [])
        output = [f"- {f['name']} ({f['mimeType']}) | ID: {f['id']}" for f in files]
        return "\n".join(output) if output else "No files found.", None, None

    elif action == "search":
        q = data.get("query", "trashed=false")
        # Wrap plain keyword searches automatically
        if "=" not in q and "contains" not in q:
            q = f"name contains '{q}' and trashed=false"
        res = svc.files().list(
            q=q,
            fields="files(id, name, mimeType, size, modifiedTime)",
            pageSize=20
        ).execute()
        files = res.get("files", [])
        output = [f"- {f['name']} ({f['mimeType']}) | ID: {f['id']}" for f in files]
        return "\n".join(output) if output else "No files found.", None, None

    elif action == "download":
        meta = svc.files().get(fileId=data["file_id"], fields="name, mimeType").execute()
        fh = io.BytesIO()
        request = svc.files().get_media(fileId=data["file_id"])
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return f"Downloaded {meta['name']}", fh.getvalue(), meta["name"]

    elif action == "upload":
        content = data["content"].encode() if isinstance(data["content"], str) else data["content"]
        fh = io.BytesIO(content)
        media = MediaIoBaseUpload(fh, mimetype=data.get("mime_type", "text/plain"))
        file_meta = {"name": data["name"]}
        if data.get("parent_id"):
            file_meta["parents"] = [data["parent_id"]]
        result = svc.files().create(body=file_meta, media_body=media, fields="id, name").execute()
        return f"Uploaded: {result['name']} (ID: {result['id']})", None, None

    elif action == "copy":
        body = {}
        if data.get("name"):
            body["name"] = data["name"]
        if data.get("parent_id"):
            body["parents"] = [data["parent_id"]]
        result = svc.files().copy(fileId=data["file_id"], body=body, fields="id, name").execute()
        return f"Copied as: {result['name']} (ID: {result['id']})", None, None

    elif action == "move":
        # Get current parents first
        meta = svc.files().get(fileId=data["file_id"], fields="parents").execute()
        current_parents = ",".join(meta.get("parents", []))
        result = svc.files().update(
            fileId=data["file_id"],
            addParents=data["new_parent_id"],
            removeParents=current_parents,
            fields="id, name, parents"
        ).execute()
        return f"Moved: {result['name']} to folder ID {data['new_parent_id']}", None, None

    elif action == "rename":
        result = svc.files().update(
            fileId=data["file_id"],
            body={"name": data["name"]},
            fields="id, name"
        ).execute()
        return f"Renamed to: {result['name']}", None, None

    elif action == "create_folder":
        meta = {"name": data["name"], "mimeType": "application/vnd.google-apps.folder"}
        if data.get("parent_id"):
            meta["parents"] = [data["parent_id"]]
        result = svc.files().create(body=meta, fields="id, name").execute()
        return f"Folder created: {result['name']} (ID: {result['id']})", None, None

    elif action == "delete":
        svc.files().trash(fileId=data["file_id"]).execute()
        return "File moved to trash.", None, None

    elif action == "delete_permanent":
        svc.files().delete(fileId=data["file_id"]).execute()
        return "File permanently deleted.", None, None

    elif action == "get_share_link":
        if data.get("anyone_can_view", False):
            svc.permissions().create(
                fileId=data["file_id"],
                body={"type": "anyone", "role": "reader"},
            ).execute()
        meta = svc.files().get(
            fileId=data["file_id"],
            fields="webViewLink, webContentLink, name"
        ).execute()
        link = meta.get("webViewLink") or meta.get("webContentLink") or "No link available."
        return f"Share link for '{meta.get('name', '')}': {link}", None, None

    elif action == "share":
        role = data.get("role", "reader")  # reader, writer, commenter
        svc.permissions().create(
            fileId=data["file_id"],
            body={
                "type": "user",
                "role": role,
                "emailAddress": data["email"],
            },
            sendNotificationEmail=True,
        ).execute()
        return f"Shared with {data['email']} as {role}.", None, None

    return f"Unknown action: {action}", None, None