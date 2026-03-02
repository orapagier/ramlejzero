<div align="center">
  <img src="logo.png" alt="ramlejzero Logo" width="200"/>
  <h1>ramlejzero⚡</h1>
  <p><strong>A self-hosted, continuous AI agent for automating your digital life.</strong></p>
  <p>
    <a href="#-quick-start">Quick Start</a> •
    <a href="#-the-zero-promise">Features</a> •
    <a href="#-everything-online-scope">Supported Services</a> •
    <a href="#-architecture">Architecture</a>
  </p>
</div>

> **ramlej** *(reverse of Jelmar)*: A nod to $\mathbb{R}$, the set of real numbers — representing continuous, unbounded AI computation.
> **zero**: Zero friction. Zero boundaries. Zero hassle. Zero limit. Managing your entire digital life from a single surface.

ramlejzero is a self-hosted, Docker-first AI agent that connects to your cloud services and automates workflows through a Telegram bot and web dashboard. 

**Extensibility made simple:** Drop a `.py` file in `/tools` and your agent instantly gains a new capability. No recompilation or restarts required.

---

## ✨ The "Zero" Promise

| Feature | The "Zero" Benefit |
|---|---|
| **Docker-First** | Zero installation headaches and consistent runtime environments. |
| **Modular Tools** | Zero limits on what you can automate. Plug-and-play architecture. |
| **Self-Hosted** | Zero data leaving your hands. Total privacy and control. |

---

## 🚀 Quick Start

### Recommended: Docker (Zero Hassle)

Ensure you have [Docker](https://docs.docker.com/get-docker/) and Docker Compose installed.

```bash
# 1. Clone the repository
git clone https://github.com/yourusername/ramlejzero.git
cd ramlejzero

# 2. Start the services
docker compose up -d --build
```

Access the Web UI at `https://your-domain.com` or `http://localhost:8000`.

### Manual (Bare Metal)

Requires Python 3.11+.

```bash
pip install -r requirements.txt
python main.py
```

> **Note:** In bare-metal mode, you are responsible for managing your own environment variables, daemonizing the process, and managing dependencies.

---

## 🌐 "Everything Online" Scope

ramlejzero is designed to reach across your entire digital life. Connect services by dropping credentials into `config/apis.yaml` and enabling the corresponding tool in `/tools`.

### Supported Ecosystems

*   **Cloud Productivity**: Google Workspace (Gmail, Drive, Calendar), Microsoft 365 (OneDrive, Outlook via Graph API)
*   **Infrastructure & DevOps**: SSH across Linux servers, Docker management, `systemctl`, log tailing, and custom integrations for Cloudflare, AWS, DigitalOcean, Azure.
*   **Communication & Social**: Telegram (primary bot + webhook interface), Discord, Slack, X/Twitter, LinkedIn.
*   **Custom Integrations**: Any REST API can be plugged in by adding a `.py` file to the `/tools` folder.
*   **Everything Online** Just give **ramlejzero** a tool

---

## 🏗️ Architecture

```text
ramlejzero/
├── agent.py              # Main execution loop & tool orchestration
├── main.py               # FastAPI server, Telegram webhook, Web UI, OAuth routes
├── core/
│   ├── model_router.py   # Multi-model fallback chain (Claude, Gemini, GPT, etc.)
│   ├── tool_router.py    # 3-tier tool selector (regex → binary LLM → passthrough)
│   ├── config_loader.py  # Hot-reloadable YAML config
│   ├── rate_limiter.py   # Per-API rate limiting
│   └── logger.py         # Structured run logging
├── auth/                 # OAuth flows & token management (Google, Microsoft)
├── tools/                # ← Drop .py tool files here, no rebuild needed
├── config/               # ← Edit on host, reflects immediately
└── web_ui/               # Dashboard: tools, models, logs, config, auth
```

### Innovative Tool Router

ramlejzero uses a 3-tier zero-waste routing system to activate tools precisely and cost-effectively: Token-efficient

1.  **Tier 1: Regex (0 tokens)** — High-precision pattern matching for instant, free resolution.
2.  **Tier 2: Binary LLM (~60 tokens)** — Fires only on ambiguity or when no regex matches.
3.  **Tier 3: Passthrough (0 extra)** — All context sent to the main agent for complex decision-making.

This ensures low token costs for common tasks while maintaining capabilities for complex reasoning.

---

## 📱 Universal Device Bridging *(Pro — Separate Purchase)*

While ramlejzero Core handles the Cloud/Web layer, local physical hardware control requires the **ramlejzero companion agents** (available separately):

- **ramlejzero for Windows**: GUI automation, screenshots, file system access, powershell, application control, anything you want to do with your windows machine.
- **ramlejzero for Android**: SMS, calls, notifications, GPS, and app launching, anything that android allows for non-root devices, and anything you want for rooted devices.

This separation keeps the Core lightweight and secure while enabling deep-system access when desired. *Contact for pricing.*

---

## 🔐 Security & Privacy

- **Self-Hosted Data Sovereignty**: ramlejzero runs entirely on your hardware. Your personal data never touches our infrastructure.
- **Local Credentials**: All OAuth tokens and API keys live solely in `auth/` and `config/` (ignored by Git).
- **Standardized Auth**: Microsoft and Google integrations use standard OAuth 2.0 with local, silent token auto-refresh.
- **Protected Web UI**: Dashboard endpoint is shielded by HTTP Basic Auth (configurable via `config/settings.yaml`).

---

## ⚙️ Configuration

All configuration is hot-reloadable from the Web UI (`/config` tab) or via direct file edits:

| File | Purpose |
|---|---|
| `config/settings.yaml` | Agent behavior, timezone, Telegram bot token, Web UI settings |
| `config/apis.yaml` | API keys, OAuth credentials, service endpoints |
| `config/models.yaml` | AI model priority, fallback chain, provider API keys |
| `config/system_prompt.txt` | The agent's personality, context, and core instructions |

---

## 🧩 Writing a Custom Tool

Create a `.py` file with the following structure and drop it into `/tools`:

```python
TOOL_DEFINITION = {
    "name": "my_tool",
    "description": "What this tool does and when to use it.",
    "parameters": {
        "action": {"type": "string", "description": "The action to perform"},
        "data":   {"type": "object", "description": "Additional payload"},
    },
    "required": ["action"]
}

async def execute(action: str, data: dict = None) -> str:
    # Your implementation logic here
    return f"Executed {action} successfully"
```

Hit **Reload Config** in the dashboard, and your tool is instantly live.

---

## 📋 Requirements

- **Docker & Docker Compose** (Recommended)
- **OR** Python 3.11+
- A Telegram Bot Token (from [@BotFather](https://t.me/botfather))
- API keys for integrated services

---

## 📄 License & Credits

**ramlejzero Core** is open-source software released under the [MIT License](LICENSE).

The **ramlejzero Companion Agents** are proprietary and available under a separate commercial license. See [COMPANION_LICENSE.md](COMPANION_LICENSE.md) for details.

Built with passion — because the best personal AI agent is the one you control entirely.

<div align="center">
  <sub><i>ramlejzero: Where r meets zero.</i></sub>
</div>
