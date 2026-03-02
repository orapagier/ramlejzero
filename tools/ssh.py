import asyncssh
from core.config_loader import get_apis

TOOL_DEFINITION = {
    "name": "ssh_tool",
    "description": "Execute bash commands on Linux servers via SSH. Use for server management, Docker, services, file operations. Provide `server_name` (e.g server1, server2) to pick a specific server defined in apis.yaml.",
    "examples": [
        "check if the site is up",
        "what's running on port 80",
        "tail the logs",
        "free up disk space on the server1",
    ],
    "parameters": {
        "command": {
            "type": "string",
            "description": "The bash command to execute on the server"
        },
        "server_name": {
            "type": "string",
            "description": "Optional name of the server to connect to (e.g., 'server1', 'server2'). If not specified, ASK the user which server to connect to."
        }
    },
    "required": ["command"]
}


async def execute(command: str, server_name: str = None) -> str:
    cfg = get_apis().get("ssh", {})
    servers = cfg.get("servers", {})

    if not servers:
        return "SSH error: No SSH servers defined in apis.yaml. Please configure your 'servers' block."

    if not server_name:
        return f"Please specify a server to connect to. Available servers: {', '.join(servers.keys())}"

    if server_name not in servers:
        return f"SSH error: Server '{server_name}' is not defined in apis.yaml. Available servers: {', '.join(servers.keys())}"
        
    server_cfg = servers[server_name]

    try:
        async with asyncssh.connect(
            server_cfg["host"],
            username=server_cfg.get("user", "root"),
            client_keys=[server_cfg.get("key_path", "~/.ssh/id_rsa")],
            known_hosts=None
        ) as conn:
            result = await conn.run(command, timeout=60)
            if result.exit_status != 0:
                return f"Error on {server_name} (exit {result.exit_status}):\n{result.stderr.strip() or result.stdout.strip()}"
            return result.stdout.strip() or f"Command executed successfully on {server_name} (no output)"
    except Exception as e:
        return f"SSH error on {server_name}: {e}"
