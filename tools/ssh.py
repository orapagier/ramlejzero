import asyncssh
import os
from core.config_loader import get_apis

TOOL_DEFINITION = {
    "name": "ssh_tool",
    "description": (
        "Execute bash commands and transfer files on Linux servers via SSH/SFTP. "
        "Use for server management, Docker, services, file operations, "
        "and uploading/downloading files. Provide server_name to pick a specific "
        "server from apis.yaml. Use list_servers to discover available servers."
    ),
    "examples": [
        "check if the site is up",
        "what's running on port 80",
        "tail the logs",
        "free up disk space on server1",
        "upload this config file to the server",
        "download the nginx log from server2",
        "what servers do I have",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": (
                "Action to perform: "
                "'run' - execute a bash command; "
                "'upload_file' - upload a file to the server via SFTP; "
                "'download_file' - download a file from the server via SFTP; "
                "'list_servers' - list all configured servers (no server_name needed)."
            ),
            "enum": ["run", "upload_file", "download_file", "list_servers"]
        },
        "command": {
            "type": "string",
            "description": "Bash command to execute on the server. Required for action='run'."
        },
        "server_name": {
            "type": "string",
            "description": (
                "Name of the server to connect to (e.g. 'server1', 'server2'). "
                "Required for all actions except list_servers. "
                "If unsure, use list_servers first or ask the user."
            )
        },
        "remote_path": {
            "type": "string",
            "description": (
                "Remote file path on the server. "
                "Required for upload_file and download_file. "
                "Example: '/var/www/html/config.json' or '/home/user/logs/nginx.log'."
            )
        },
        "local_content": {
            "type": "string",
            "description": (
                "File content to upload as a string. "
                "Required for upload_file. "
                "For binary files, provide base64-encoded content."
            )
        }
    },
    "required": ["action"]
}


async def _get_conn(server_cfg: dict):
    """Create and return an asyncssh connection."""
    return await asyncssh.connect(
        server_cfg["host"],
        port=server_cfg.get("port", 22),
        username=server_cfg.get("user", "root"),
        client_keys=[server_cfg.get("key_path", "~/.ssh/id_rsa")],
        known_hosts=None,
        connect_timeout=10,
    )


async def execute(
    action: str,
    server_name: str = None,
    command: str = None,
    remote_path: str = None,
    local_content: str = None,
) -> tuple:
    """Returns (text_result, file_bytes, filename)"""
    cfg = get_apis().get("ssh", {})
    servers = cfg.get("servers", {})

    # ── List servers ──────────────────────────────────────────────────────────
    if action == "list_servers":
        if not servers:
            return "No SSH servers configured in apis.yaml.", None, None
        lines = []
        for name, s in servers.items():
            lines.append(
                f"- {name}: {s.get('user', 'root')}@{s['host']}:{s.get('port', 22)}"
            )
        return "\n".join(lines), None, None

    # ── Validate server for all other actions ─────────────────────────────────
    if not servers:
        return "SSH error: No SSH servers defined in apis.yaml.", None, None
    if not server_name:
        return (
            f"Please specify a server. Available: {', '.join(servers.keys())}. "
            f"Or use action='list_servers' to see details.", None, None
        )
    if server_name not in servers:
        return (
            f"SSH error: Server '{server_name}' not found. "
            f"Available: {', '.join(servers.keys())}.", None, None
        )

    server_cfg = servers[server_name]

    # ── Run bash command ──────────────────────────────────────────────────────
    if action == "run":
        if not command:
            return "SSH error: 'command' is required for action='run'.", None, None
        try:
            async with asyncssh.connect(
                server_cfg["host"],
                port=server_cfg.get("port", 22),
                username=server_cfg.get("user", "root"),
                client_keys=[server_cfg.get("key_path", "~/.ssh/id_rsa")],
                known_hosts=None,
                connect_timeout=10,
            ) as conn:
                result = await conn.run(command, timeout=60)
                if result.exit_status != 0:
                    return (
                        f"Error on {server_name} (exit {result.exit_status}):\n"
                        f"{result.stderr.strip() or result.stdout.strip()}",
                        None, None
                    )
                output = result.stdout.strip() or f"Command executed on {server_name} (no output)."
                return output, None, None
        except asyncssh.TimeoutError:
            return (
                f"SSH error on {server_name}: Connection timed out after 10 seconds. "
                f"Server may be offline or unreachable.", None, None
            )
        except Exception as e:
            return f"SSH error on {server_name}: {e}", None, None

    # ── Upload file via SFTP ──────────────────────────────────────────────────
    elif action == "upload_file":
        if not remote_path:
            return "SSH error: 'remote_path' is required for action='upload_file'.", None, None
        if local_content is None:
            return "SSH error: 'local_content' is required for action='upload_file'.", None, None
        try:
            async with asyncssh.connect(
                server_cfg["host"],
                port=server_cfg.get("port", 22),
                username=server_cfg.get("user", "root"),
                client_keys=[server_cfg.get("key_path", "~/.ssh/id_rsa")],
                known_hosts=None,
                connect_timeout=10,
            ) as conn:
                async with conn.start_sftp_client() as sftp:
                    content_bytes = (
                        local_content.encode()
                        if isinstance(local_content, str)
                        else local_content
                    )
                    # Ensure remote directory exists
                    remote_dir = os.path.dirname(remote_path)
                    if remote_dir:
                        try:
                            await sftp.makedirs(remote_dir, exist_ok=True)
                        except Exception:
                            pass  # Directory may already exist
                    async with sftp.open(remote_path, "wb") as f:
                        await f.write(content_bytes)
            filename = os.path.basename(remote_path)
            return (
                f"File uploaded to {server_name}:{remote_path} "
                f"({len(content_bytes)} bytes).", None, None
            )
        except asyncssh.TimeoutError:
            return f"SFTP error on {server_name}: Connection timed out.", None, None
        except Exception as e:
            return f"SFTP upload error on {server_name}: {e}", None, None

    # ── Download file via SFTP ────────────────────────────────────────────────
    elif action == "download_file":
        if not remote_path:
            return "SSH error: 'remote_path' is required for action='download_file'.", None, None
        try:
            async with asyncssh.connect(
                server_cfg["host"],
                port=server_cfg.get("port", 22),
                username=server_cfg.get("user", "root"),
                client_keys=[server_cfg.get("key_path", "~/.ssh/id_rsa")],
                known_hosts=None,
                connect_timeout=10,
            ) as conn:
                async with conn.start_sftp_client() as sftp:
                    async with sftp.open(remote_path, "rb") as f:
                        file_bytes = await f.read()
            filename = os.path.basename(remote_path)
            return (
                f"Downloaded {filename} from {server_name}:{remote_path} "
                f"({len(file_bytes)} bytes).",
                file_bytes,
                filename
            )
        except asyncssh.TimeoutError:
            return f"SFTP error on {server_name}: Connection timed out.", None, None
        except Exception as e:
            return f"SFTP download error on {server_name}: {e}", None, None

    return f"Unknown action: {action}", None, None
