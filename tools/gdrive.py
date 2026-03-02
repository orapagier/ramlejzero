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
        "copy, move, rename, create_folder, delete (trash), restore, list_trash, "
        "delete_permanent, get_file_info, get_share_link, share."
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
        "restore that file I trashed",
        "what's in my trash",
        "get info about this file",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": (
                "Action to perform: "
                "'list' - list files; "
                "'search' - search by Drive query; "
                "'upload' - upload a file; "
                "'download' - download a file (auto-exports Google Docs/Sheets/Slides); "
                "'copy' - copy a file; "
                "'move' - move file to another folder; "
                "'rename' - rename a file or folder; "
                "'create_folder' - create a new folder; "
                "'get_file_info' - get metadata without downloading; "
                "'delete' - move file to trash; "
                "'restore' - restore a trashed file; "
                "'list_trash' - list files currently in trash; "
                "'delete_permanent' - permanently delete a file; "
                "'get_share_link' - get a shareable link; "
                "'share' - share with a specific user."
            ),
            "enum": [
                "list", "search", "upload", "download",
                "copy", "move", "rename", "create_folder",
                "get_file_info", "delete", "restore", "list_trash",
                "delete_permanent", "get_share_link", "share"
            ]
        },
        "data": {
            "type": "object",
            "description": (
                "Action-specific data:\n"
                "list: {query?} raw Drive query (default: trashed=false).\n"
                "search: {query} raw Drive query. ALWAYS use 'name contains' not 'name =' for name searches, "
                "and NEVER include file extensions. Example: \"name contains 'report' and trashed=false\".\n"
                "upload: {name, content, mime_type?, parent_id?}.\n"
                "download: {file_id}. Google Docs/Sheets/Slides auto-exported as .docx/.xlsx/.pptx.\n"
                "copy: {file_id, name?, parent_id?}.\n"
                "move: {file_id, new_parent_id}.\n"
                "rename: {file_id, name}.\n"
                "create_folder: {name, parent_id?}.\n"
                "get_file_info: {file_id} — returns name, mimeType, size, owner, modifiedTime, parents, sharing status.\n"
                "delete: {file_id} — moves to trash.\n"
                "restore: {file_id} — removes trashed=true, restores file to Drive.\n"
                "list_trash: {} — lists all files currently in trash.\n"
                "delete_permanent: {file_id}.\n"
                "get_share_link: {file_id, anyone_can_view?} — anyone_can_view=true makes it public.\n"
                "share: {file_id, email, role?} — role: reader/writer/commenter (default: reader)."
            )
        }
    },
    "required": ["action"]
}

# Google Workspace files must be exported, not downloaded with get_media().
# Using get_media() on .gdoc/.gsheet/.gslides returns a 403 error.
_WORKSPACE_EXPORT = {
    "application/vnd.google-apps.document":
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation":
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing":
        ("image/png", ".png"),
    "application/vnd.google-apps.script":
        ("application/vnd.google-apps.script+json", ".json"),
}


def _service():
    return build("drive", "v3", credentials=get_credentials())


async def execute(action: str, data: dict = None) -> tuple:
    """Returns (text_result, file_bytes, filename)"""
    await check_and_record("google_apis", wait=True)
    svc = _service()
    data = data or {}

    # ── List ──────────────────────────────────────────────────────────────────
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

    # ── Search ────────────────────────────────────────────────────────────────
    elif action == "search":
        q = data.get("query", "trashed=false")
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

    # ── Download (with Workspace export) ──────────────────────────────────────
    elif action == "download":
        meta = svc.files().get(fileId=data["file_id"], fields="name, mimeType").execute()
        mime = meta["mimeType"]
        name = meta["name"]
        fh = io.BytesIO()

        if mime in _WORKSPACE_EXPORT:
            export_mime, ext = _WORKSPACE_EXPORT[mime]
            request = svc.files().export_media(fileId=data["file_id"], mimeType=export_mime)
            if not name.endswith(ext):
                name = name + ext
        else:
            request = svc.files().get_media(fileId=data["file_id"])

        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return f"Downloaded {name}", fh.getvalue(), name

    # ── Upload ────────────────────────────────────────────────────────────────
    elif action == "upload":
        content = data["content"].encode() if isinstance(data["content"], str) else data["content"]
        fh = io.BytesIO(content)
        media = MediaIoBaseUpload(fh, mimetype=data.get("mime_type", "text/plain"))
        file_meta = {"name": data["name"]}
        if data.get("parent_id"):
            file_meta["parents"] = [data["parent_id"]]
        result = svc.files().create(body=file_meta, media_body=media, fields="id, name").execute()
        return f"Uploaded: {result['name']} (ID: {result['id']})", None, None

    # ── Copy ──────────────────────────────────────────────────────────────────
    elif action == "copy":
        body = {}
        if data.get("name"):
            body["name"] = data["name"]
        if data.get("parent_id"):
            body["parents"] = [data["parent_id"]]
        result = svc.files().copy(fileId=data["file_id"], body=body, fields="id, name").execute()
        return f"Copied as: {result['name']} (ID: {result['id']})", None, None

    # ── Move ──────────────────────────────────────────────────────────────────
    elif action == "move":
        meta = svc.files().get(fileId=data["file_id"], fields="parents").execute()
        current_parents = ",".join(meta.get("parents", []))
        result = svc.files().update(
            fileId=data["file_id"],
            addParents=data["new_parent_id"],
            removeParents=current_parents,
            fields="id, name, parents"
        ).execute()
        return f"Moved: {result['name']} to folder ID {data['new_parent_id']}", None, None

    # ── Rename ────────────────────────────────────────────────────────────────
    elif action == "rename":
        result = svc.files().update(
            fileId=data["file_id"],
            body={"name": data["name"]},
            fields="id, name"
        ).execute()
        return f"Renamed to: {result['name']}", None, None

    # ── Create folder ─────────────────────────────────────────────────────────
    elif action == "create_folder":
        meta = {"name": data["name"], "mimeType": "application/vnd.google-apps.folder"}
        if data.get("parent_id"):
            meta["parents"] = [data["parent_id"]]
        result = svc.files().create(body=meta, fields="id, name").execute()
        return f"Folder created: {result['name']} (ID: {result['id']})", None, None

    # ── Get file info (metadata only, no download) ────────────────────────────
    elif action == "get_file_info":
        f = svc.files().get(
            fileId=data["file_id"],
            fields="id, name, mimeType, size, createdTime, modifiedTime, "
                   "owners, parents, shared, webViewLink, trashed"
        ).execute()
        owner = f.get("owners", [{}])[0].get("emailAddress", "?")
        size = f.get("size", "N/A (Google Workspace file)")
        lines = [
            f"Name: {f['name']}",
            f"Type: {f['mimeType']}",
            f"Size: {size} bytes",
            f"Owner: {owner}",
            f"Created: {f.get('createdTime', '?')}",
            f"Modified: {f.get('modifiedTime', '?')}",
            f"Shared: {f.get('shared', False)}",
            f"Trashed: {f.get('trashed', False)}",
            f"ID: {f['id']}",
        ]
        if f.get("webViewLink"):
            lines.append(f"View link: {f['webViewLink']}")
        return "\n".join(lines), None, None

    # ── Delete (trash) ────────────────────────────────────────────────────────
    elif action == "delete":
        svc.files().trash(fileId=data["file_id"]).execute()
        return "File moved to trash.", None, None

    # ── Restore from trash ────────────────────────────────────────────────────
    elif action == "restore":
        svc.files().untrash(fileId=data["file_id"]).execute()
        meta = svc.files().get(fileId=data["file_id"], fields="name").execute()
        return f"File restored: {meta['name']}", None, None

    # ── List trash ────────────────────────────────────────────────────────────
    elif action == "list_trash":
        res = svc.files().list(
            q="trashed=true",
            fields="files(id, name, mimeType, trashedTime)",
            pageSize=20
        ).execute()
        files = res.get("files", [])
        if not files:
            return "Trash is empty.", None, None
        output = [
            f"- {f['name']} ({f['mimeType']}) | Trashed: {f.get('trashedTime', '?')} | ID: {f['id']}"
            for f in files
        ]
        return "\n".join(output), None, None

    # ── Permanent delete ──────────────────────────────────────────────────────
    elif action == "delete_permanent":
        svc.files().delete(fileId=data["file_id"]).execute()
        return "File permanently deleted.", None, None

    # ── Share link ────────────────────────────────────────────────────────────
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

    # ── Share with user ───────────────────────────────────────────────────────
    elif action == "share":
        role = data.get("role", "reader")
        svc.permissions().create(
            fileId=data["file_id"],
            body={"type": "user", "role": role, "emailAddress": data["email"]},
            sendNotificationEmail=True,
        ).execute()
        return f"Shared with {data['email']} as {role}.", None, None

    return f"Unknown action: {action}", None, None
