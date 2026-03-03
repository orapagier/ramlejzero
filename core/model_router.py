"""
model_router.py — Universal Provider Router with Round-Robin Rotation
======================================================================
Architecture:
  - Models sharing the same priority form a ROTATION GROUP (round-robin)
  - When a rotation group is fully exhausted, the next priority tier activates
  - All providers normalize to a single internal response format
  - Message history stored in Anthropic format; converted per-provider at call time

Provider support:
  Native:          anthropic
  OpenAI-compat:   google, openai, groq, cerebras, nvidia, openrouter, ollama, custom
                   (anything with a base_url works — just set provider: openai)

models.yaml fields:
  name      → unique label (required)
  model_id  → model string sent to API (optional — falls back to name if omitted)
  provider  → anthropic | google | openai
  api_key   → your key
  base_url  → optional override (required for groq/cerebras/openrouter/ollama)
  priority  → same number = rotation group; higher = lower priority fallback tier
  max_tokens → per-model cap (default: 4096)
  enabled   → false to skip without deleting
"""

import json
import time
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Any

import anthropic
from openai import AsyncOpenAI

from core.config_loader import get_settings

logger = logging.getLogger("model_router")

# ─────────────────────────────────────────────────────────────────────────────
# Provider base URLs for OpenAI-compat providers
# ─────────────────────────────────────────────────────────────────────────────

_PROVIDER_BASE_URLS = {
    "google":     "https://generativelanguage.googleapis.com/v1beta/openai/",
    "groq":       "https://api.groq.com/openai/v1",
    "cerebras":   "https://api.cerebras.ai/v1",
    "nvidia":     "https://integrate.api.nvidia.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama":     "http://localhost:11434/v1",
    "openai":     None,  # uses OpenAI default
}


# ─────────────────────────────────────────────────────────────────────────────
# Provider rate limit baselines (Free / Paid tiers)
# These are reference values only — actual enforced limits come from headers.
# None = not documented / not applicable
# ─────────────────────────────────────────────────────────────────────────────

PROVIDER_BASELINES = {
    "groq": {
        "free": {
            "req_per_min": 30,    "tokens_per_min": 6_000,
            "req_per_hour": None, "tokens_per_hour": None,
            "req_per_day": 14_400, "tokens_per_day": 500_000,
        },
        "paid": {
            "req_per_min": 6_000,  "tokens_per_min": 200_000,
            "req_per_hour": None,  "tokens_per_hour": None,
            "req_per_day": None,   "tokens_per_day": None,
        },
        "note": "Limits vary per model — shown values are typical for most models",
        "header_style": "openai",  # x-ratelimit-remaining-requests / reset in Xs format
    },
    "cerebras": {
        "free": {
            "req_per_min": 30,       "tokens_per_min": 60_000,
            "req_per_hour": 900,     "tokens_per_hour": 1_000_000,
            "req_per_day": None,     "tokens_per_day": None,
        },
        "paid": {
            "req_per_min": 240,      "tokens_per_min": 1_000_000,
            "req_per_hour": None,    "tokens_per_hour": None,
            "req_per_day": None,     "tokens_per_day": None,
        },
        "note": "Reset times returned as seconds in headers",
        "header_style": "cerebras",  # x-ratelimit-remaining-tokens-per-minute etc.
    },
    "anthropic": {
        "free": {
            "req_per_min": 5,     "tokens_per_min": 25_000,
            "req_per_hour": None, "tokens_per_hour": None,
            "req_per_day": None,  "tokens_per_day": None,
        },
        "paid": {
            "req_per_min": 50,    "tokens_per_min": 50_000,
            "req_per_hour": None, "tokens_per_hour": None,
            "req_per_day": None,  "tokens_per_day": None,
        },
        "note": "Paid values shown for Tier 1; escalates through Tier 4",
        "header_style": "anthropic",
    },
    "openai": {
        "free": {
            "req_per_min": 3,     "tokens_per_min": 40_000,
            "req_per_hour": None, "tokens_per_hour": None,
            "req_per_day": None,  "tokens_per_day": None,
        },
        "paid": {
            "req_per_min": 500,   "tokens_per_min": 200_000,
            "req_per_hour": None, "tokens_per_hour": None,
            "req_per_day": None,  "tokens_per_day": None,
        },
        "note": "Paid values for Tier 1; varies significantly by model",
        "header_style": "openai",
    },
    "google": {
        "free": {
            "req_per_min": 15,     "tokens_per_min": 1_000_000,
            "req_per_hour": None,  "tokens_per_hour": None,
            "req_per_day": 1_500,  "tokens_per_day": None,
        },
        "paid": {
            "req_per_min": 2_000,  "tokens_per_min": 4_000_000,
            "req_per_hour": None,  "tokens_per_hour": None,
            "req_per_day": None,   "tokens_per_day": None,
        },
        "note": "Gemini Flash limits shown; other models differ",
        "header_style": "openai",
    },
    "openrouter": {
        "free": {
            "req_per_min": 20,    "tokens_per_min": None,
            "req_per_hour": None, "tokens_per_hour": None,
            "req_per_day": 200,   "tokens_per_day": None,
        },
        "paid": {
            "req_per_min": 600,   "tokens_per_min": None,
            "req_per_hour": None, "tokens_per_hour": None,
            "req_per_day": None,  "tokens_per_day": None,
        },
        "note": "Free model limits; paid model limits vary by model credits",
        "header_style": "openai",
    },
    "nvidia": {
        "free": {
            "req_per_min": 40,    "tokens_per_min": None,
            "req_per_hour": None, "tokens_per_hour": None,
            "req_per_day": None,  "tokens_per_day": None,
        },
        "paid": {
            "req_per_min": None,  "tokens_per_min": None,
            "req_per_hour": None, "tokens_per_hour": None,
            "req_per_day": None,  "tokens_per_day": None,
        },
        "note": "API catalog limits; paid tiers billed per token",
        "header_style": "openai",
    },
    "ollama": {
        "free": None,
        "paid": None,
        "note": "Local deployment — no enforced rate limits",
        "header_style": None,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ContentBlock:
    type: str                       # "text" | "tool_use"
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class UsageInfo:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class RateLimitSnapshot:
    """Live rate-limit data parsed from response headers."""
    # Per-minute window
    req_limit_per_min: int | None = None
    req_remaining_per_min: int | None = None
    req_reset_per_min: str | None = None       # ISO ts or human-readable "Xs"
    tokens_limit_per_min: int | None = None
    tokens_remaining_per_min: int | None = None
    tokens_reset_per_min: str | None = None
    # Per-hour window (Cerebras)
    req_limit_per_hour: int | None = None
    req_remaining_per_hour: int | None = None
    tokens_limit_per_hour: int | None = None
    tokens_remaining_per_hour: int | None = None
    tokens_reset_per_hour: str | None = None
    # Catch-all / generic (Anthropic uses un-windowed headers)
    req_limit: int | None = None
    req_remaining: int | None = None
    req_reset: str | None = None
    tokens_limit: int | None = None
    tokens_remaining: int | None = None
    tokens_reset: str | None = None
    # Meta
    last_updated: float = 0.0   # time.time()

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class UnifiedResponse:
    content: list[ContentBlock]
    stop_reason: str                # "end_turn" | "tool_use"
    usage: UsageInfo


@dataclass
class ModelRecord:
    name: str
    provider: str
    model_id: str
    api_key: str
    base_url: str | None
    priority: int
    max_tokens: int
    enabled: bool
    role: str = ""
    # Role controls which pool this model belongs to:
    #   ""           → general pool  (Pass 1 round-robin, used by main agent & planner)
    #   "paid_model" → paid pool     (Pass 2 ultimate fallback, shared by all callers)
    #   "router"     → router pool   (Tier 2 binary LLM)
    #   "<anything>" → custom pool   (future features — just set role in models.yaml
    #                                 and pass role="<anything>" to call_llm)

    # Runtime state
    status: str = "available"
    rate_limit_reset_at: str | None = None
    consecutive_errors: int = 0
    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    # Live rate-limit snapshot from response headers
    rl_snapshot: RateLimitSnapshot = field(default_factory=RateLimitSnapshot)

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        if self.status == "rate_limited":
            if self.rate_limit_reset_at:
                try:
                    reset_ts = time.mktime(
                        time.strptime(self.rate_limit_reset_at, "%Y-%m-%dT%H:%M:%SZ")
                    )
                    if time.time() > reset_ts:
                        self.status = "available"
                        self.rate_limit_reset_at = None
                        self.consecutive_errors = 0
                        return True
                except Exception:
                    pass
            return False
        return self.status != "unavailable"

    def mark_rate_limited(self):
        settings = get_settings()
        cooldown = settings.get("models", {}).get("rate_limit_cooldown_minutes", 60)
        reset_seconds = cooldown * 60
        self.status = "rate_limited"
        reset_at = time.gmtime(time.time() + reset_seconds)
        self.rate_limit_reset_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", reset_at)
        logger.warning(f"{self.name} rate limited — resets at {self.rate_limit_reset_at}")

    def mark_error(self):
        settings = get_settings()
        threshold = settings.get("models", {}).get("error_threshold", 3)
        self.consecutive_errors += 1
        if self.consecutive_errors >= threshold:
            self.status = "unavailable"
            logger.error(f"{self.name} unavailable after {self.consecutive_errors} errors")

    def mark_success(self, input_tokens: int, output_tokens: int):
        self.consecutive_errors = 0
        self.status = "available"
        self.total_calls += 1
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens


# ─────────────────────────────────────────────────────────────────────────────
# Rate limit header parsers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_openai_rl_headers(headers: dict) -> RateLimitSnapshot:
    """
    Parse OpenAI-style headers (also used by Groq, Google, OpenRouter, Nvidia).
    Header names: x-ratelimit-limit-requests, x-ratelimit-remaining-requests,
                  x-ratelimit-reset-requests (value like "1s", "59.5s", "1m30s")
    """
    def _int(key: str) -> int | None:
        v = headers.get(key)
        return int(v) if v and v.isdigit() else None

    snap = RateLimitSnapshot(last_updated=time.time())
    snap.req_limit_per_min      = _int("x-ratelimit-limit-requests")
    snap.req_remaining_per_min  = _int("x-ratelimit-remaining-requests")
    snap.req_reset_per_min      = headers.get("x-ratelimit-reset-requests")
    snap.tokens_limit_per_min   = _int("x-ratelimit-limit-tokens")
    snap.tokens_remaining_per_min = _int("x-ratelimit-remaining-tokens")
    snap.tokens_reset_per_min   = headers.get("x-ratelimit-reset-tokens")
    return snap


def _parse_cerebras_rl_headers(headers: dict) -> RateLimitSnapshot:
    """
    Parse Cerebras-style headers.
    Header names: x-ratelimit-limit-tokens-per-minute,
                  x-ratelimit-remaining-tokens-per-minute,
                  x-ratelimit-reset-tokens-per-minute (seconds as int),
                  x-ratelimit-limit-tokens-per-hour, etc.
    """
    def _int(key: str) -> int | None:
        v = headers.get(key)
        try:
            return int(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    snap = RateLimitSnapshot(last_updated=time.time())
    snap.req_limit_per_min       = _int("x-ratelimit-limit-requests-per-minute")
    snap.req_remaining_per_min   = _int("x-ratelimit-remaining-requests-per-minute")
    snap.tokens_limit_per_min    = _int("x-ratelimit-limit-tokens-per-minute")
    snap.tokens_remaining_per_min = _int("x-ratelimit-remaining-tokens-per-minute")
    reset_min = headers.get("x-ratelimit-reset-tokens-per-minute")
    snap.tokens_reset_per_min    = f"{reset_min}s" if reset_min else None
    snap.tokens_limit_per_hour   = _int("x-ratelimit-limit-tokens-per-hour")
    snap.tokens_remaining_per_hour = _int("x-ratelimit-remaining-tokens-per-hour")
    reset_hr = headers.get("x-ratelimit-reset-tokens-per-hour")
    snap.tokens_reset_per_hour   = f"{reset_hr}s" if reset_hr else None
    return snap


def _parse_anthropic_rl_headers(headers: dict) -> RateLimitSnapshot:
    """
    Parse Anthropic-style headers.
    Header names: anthropic-ratelimit-requests-limit,
                  anthropic-ratelimit-requests-remaining,
                  anthropic-ratelimit-requests-reset (ISO timestamp)
    """
    def _int(key: str) -> int | None:
        v = headers.get(key)
        try:
            return int(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    snap = RateLimitSnapshot(last_updated=time.time())
    snap.req_limit      = _int("anthropic-ratelimit-requests-limit")
    snap.req_remaining  = _int("anthropic-ratelimit-requests-remaining")
    snap.req_reset      = headers.get("anthropic-ratelimit-requests-reset")
    snap.tokens_limit   = _int("anthropic-ratelimit-tokens-limit")
    snap.tokens_remaining = _int("anthropic-ratelimit-tokens-remaining")
    snap.tokens_reset   = headers.get("anthropic-ratelimit-tokens-reset")
    return snap


def _parse_headers(provider: str, headers: dict) -> RateLimitSnapshot:
    style = PROVIDER_BASELINES.get(provider, {}).get("header_style")
    if style == "anthropic":
        return _parse_anthropic_rl_headers(headers)
    elif style == "cerebras":
        return _parse_cerebras_rl_headers(headers)
    elif style == "openai":
        return _parse_openai_rl_headers(headers)
    return RateLimitSnapshot(last_updated=time.time())


# ─────────────────────────────────────────────────────────────────────────────
# Tool format converters
# ─────────────────────────────────────────────────────────────────────────────

def _to_anthropic_tools(tool_defs: list[dict]) -> list[dict]:
    return [{
        "name": t["name"],
        "description": t.get("description", ""),
        "input_schema": {
            "type": "object",
            "properties": t.get("parameters", {}),
            "required": t.get("required", []),
        }
    } for t in tool_defs]


def _to_openai_tools(tool_defs: list[dict]) -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": {
                "type": "object",
                "properties": t.get("parameters", {}),
                "required": t.get("required", []),
            }
        }
    } for t in tool_defs]


# ─────────────────────────────────────────────────────────────────────────────
# Message history converters
# ─────────────────────────────────────────────────────────────────────────────

def _messages_to_openai(messages: list[dict], system: str) -> list[dict]:
    result = []
    if system:
        result.append({"role": "system", "content": system})

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if role == "user":
            if isinstance(content, str):
                result.append({"role": "user", "content": content})
            elif isinstance(content, list):
                if content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
                    for block in content:
                        result.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block["content"],
                        })
                else:
                    text_parts = [
                        b["text"] for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    result.append({"role": "user", "content": " ".join(text_parts)})

        elif role == "assistant":
            if isinstance(content, str):
                result.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                text = ""
                tool_calls = []
                for block in content:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            text = block.text
                        elif block.type == "tool_use":
                            tool_calls.append({
                                "id": block.id,
                                "type": "function",
                                "function": {
                                    "name": block.name,
                                    "arguments": json.dumps(block.input),
                                }
                            })
                    elif isinstance(block, dict):
                        if block.get("type") == "text":
                            text = block.get("text", "")
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "id": block["id"],
                                "type": "function",
                                "function": {
                                    "name": block["name"],
                                    "arguments": json.dumps(block.get("input", {})),
                                }
                            })

                oai_msg: dict[str, Any] = {"role": "assistant", "content": text or None}
                if tool_calls:
                    oai_msg["tool_calls"] = tool_calls
                result.append(oai_msg)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Response normalizers
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_anthropic_response(response) -> UnifiedResponse:
    blocks = []
    for block in response.content:
        if block.type == "text":
            blocks.append(ContentBlock(type="text", text=block.text))
        elif block.type == "tool_use":
            blocks.append(ContentBlock(
                type="tool_use", id=block.id, name=block.name, input=block.input
            ))
    stop = "tool_use" if response.stop_reason == "tool_use" else "end_turn"
    return UnifiedResponse(
        content=blocks, stop_reason=stop,
        usage=UsageInfo(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
    )


def _normalize_openai_response(response) -> UnifiedResponse:
    choice = response.choices[0]
    msg = choice.message
    blocks = []

    if msg.content:
        blocks.append(ContentBlock(type="text", text=msg.content))

    if msg.tool_calls:
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}
            blocks.append(ContentBlock(
                type="tool_use", id=tc.id, name=tc.function.name, input=args
            ))

    stop_reason = choice.finish_reason
    stop = "tool_use" if stop_reason in ("tool_calls", "function_call") else "end_turn"
    usage = response.usage
    return UnifiedResponse(
        content=blocks, stop_reason=stop,
        usage=UsageInfo(
            input_tokens=getattr(usage, "prompt_tokens", 0),
            output_tokens=getattr(usage, "completion_tokens", 0),
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Provider call implementations — now capturing rate-limit headers
# ─────────────────────────────────────────────────────────────────────────────

async def _call_anthropic(model: "ModelRecord", messages, system, tools, max_tokens) -> UnifiedResponse:
    client = anthropic.AsyncAnthropic(api_key=model.api_key)
    kwargs: dict = dict(model=model.model_id, max_tokens=max_tokens, messages=messages, system=system)
    if tools:
        kwargs["tools"] = _to_anthropic_tools(tools)

    try:
        raw = await client.messages.with_raw_response.create(**kwargs)
        headers = {k.lower(): v for k, v in raw.headers.items()}
        model.rl_snapshot = _parse_headers("anthropic", headers)
        response = raw.parse()
    except Exception:
        # Fallback: call without header capture
        response = await client.messages.create(**kwargs)

    return _normalize_anthropic_response(response)


async def _call_openai_compat(model: "ModelRecord", messages, system, tools, max_tokens) -> UnifiedResponse:
    base_url = model.base_url or _PROVIDER_BASE_URLS.get(model.provider)
    api_key = model.api_key or "nokey"

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    oai_messages = _messages_to_openai(messages, system)

    kwargs: dict = dict(model=model.model_id, max_tokens=max_tokens, messages=oai_messages)
    if tools:
        kwargs["tools"] = _to_openai_tools(tools)
        kwargs["tool_choice"] = "auto"

    try:
        raw = await client.chat.completions.with_raw_response.create(**kwargs)
        headers = {k.lower(): v for k, v in raw.headers.items()}
        model.rl_snapshot = _parse_headers(model.provider, headers)
        response = raw.parse()
    except Exception:
        # Fallback: call without header capture
        response = await client.chat.completions.create(**kwargs)

    return _normalize_openai_response(response)


async def _call_model(model: "ModelRecord", messages, system, tools, max_tokens) -> UnifiedResponse:
    if model.provider == "anthropic":
        return await _call_anthropic(model, messages, system, tools, max_tokens)
    else:
        return await _call_openai_compat(model, messages, system, tools, max_tokens)


# ─────────────────────────────────────────────────────────────────────────────
# Model registry and rotation state
# ─────────────────────────────────────────────────────────────────────────────

_models: list[ModelRecord] = []


def _load_models():
    global _models
    settings = get_settings()
    raw_models = settings.get("models", [])

    if isinstance(raw_models, dict):
        import os, yaml
        models_path = os.path.join(os.path.dirname(__file__), "..", "config", "models.yaml")
        if os.path.exists(models_path):
            with open(models_path, "r") as f:
                data = yaml.safe_load(f)
                raw_models = data.get("models", [])
        else:
            raw_models = []

    _models = []
    for m in raw_models:
        if not m.get("enabled", True):
            continue
        model_id = m.get("model_id") or m.get("name", "")
        _models.append(ModelRecord(
            name=m["name"],
            provider=m.get("provider", "openai"),
            model_id=model_id,
            api_key=m.get("api_key", ""),
            base_url=m.get("base_url"),
            priority=m.get("priority", 99),
            max_tokens=m.get("max_tokens", 4096),
            enabled=m.get("enabled", True),
            role=m.get("role", ""),
        ))

    _models.sort(key=lambda x: x.priority)
    providers = list({m.provider for m in _models})
    logger.info(f"Loaded {len(_models)} models | providers: {providers} | "
                f"priorities: {sorted({m.priority for m in _models})}")


def reload_models():
    global _global_index, _role_indexes
    _global_index = 0
    _role_indexes = {}
    _load_models()


# ─────────────────────────────────────────────────────────────────────────────
# Rotation state
# ─────────────────────────────────────────────────────────────────────────────

# General pool (role == "") index — shared across all no-role callers.
_global_index: int = 0

# Per-role round-robin indexes — one entry per distinct role value.
# Created on first use so new roles need no code changes.
# e.g. {"router": 2, "my_feature": 0}
_role_indexes: dict[str, int] = {}

_router_lock = asyncio.Lock()


def _pick_next_in_pool(
    pool: list[ModelRecord],
    start: int,
) -> tuple["ModelRecord | None", int]:
    """Round-robin over an arbitrary pool list starting at index `start`.
    Returns (model, next_start) or (None, start) if no model is available."""
    n = len(pool)
    for i in range(n):
        idx = (start + i) % n
        m = pool[idx]
        if m.is_available():
            return m, (idx + 1) % n
    return None, start


def _pick_next_available(start: int) -> tuple["ModelRecord | None", int]:
    """Convenience wrapper for the general pool (role == "").
    Role-tagged models are reserved exclusively for their designated callers
    and are never touched by the general round-robin."""
    general_pool = [m for m in _models if not m.role]
    return _pick_next_in_pool(general_pool, start)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def call_llm(
    messages: list[dict],
    system: str = "",
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
    role: str = "",
) -> tuple[UnifiedResponse, ModelRecord]:
    """
    Role-based three-pass model selection.

    Every model in models.yaml has a `role` field (default: "").
    Pools are strictly isolated — a model in one pool is never consumed by another.

    Pass 0 — dedicated pool  : models whose role == the requested role.
                               Round-robins within that role's pool.
                               5 role='router' models → rotates across all 5,
                               skipping rate-limited/errored ones.
                               Falls through only when ALL are exhausted.
    Pass 1 — general pool    : models with role == "" (no role set).
                               Round-robin with a global index lock.
    Pass 2 — paid/final pool : models with role == "paid_model".
                               Last resort, shared by all callers.

    Adding a new feature that needs its own model:
        1. Add `role: my_feature` to one or more entries in models.yaml.
        2. Call call_llm(..., role="my_feature") from your feature code.
        3. Done — it gets its own pool with automatic Pass 1 → Pass 2 fallback.

    Current roles in use:
        ""           → main agent, history summariser, task planner
        "router"     → Tier 2 binary tool router
        "paid_model" → ultimate fallback for all callers
    """
    global _global_index

    if not _models:
        _load_models()

    tools = tools or []

    async def _try_model(model: ModelRecord) -> UnifiedResponse | None:
        tokens = max_tokens or model.max_tokens
        try:
            logger.info(f"Calling {model.name} ({model.provider}/{model.model_id})")
            response = await _call_model(model, messages, system, tools, tokens)
            model.mark_success(response.usage.input_tokens, response.usage.output_tokens)
            logger.info(
                f"{model.name} OK | stop={response.stop_reason} | "
                f"tokens={response.usage.input_tokens}in+{response.usage.output_tokens}out"
            )
            return response
        except Exception as e:
            error_str = str(e).lower()
            is_rate_limit = any(x in error_str for x in [
                "rate limit", "429", "quota", "resource_exhausted",
                "too many requests", "ratelimit", "rate_limit"
            ])
            if is_rate_limit:
                model.mark_rate_limited()
                logger.warning(f"{model.name} rate limited — marked, skipping")
            else:
                model.mark_error()
                logger.warning(f"{model.name} error ({str(e)[:80]}) — marked, skipping")
            return None

    # ── Pass 0: Dedicated role pool — per-role round-robin ──────────────────────
    # Every distinct role gets its own rotation index (_role_indexes[role]).
    # With 5 role="router" models, calls rotate router-1 → router-2 → … → router-5
    # → router-1, skipping any that are rate-limited or errored.
    # Only when ALL models in the role pool are exhausted does it fall through.
    if role and role != "paid_model":   # paid_model is always Pass 2, never Pass 0
        role_pool = [m for m in _models if m.role == role and m.enabled]
        if role_pool:
            async with _router_lock:
                start = _role_indexes.get(role, 0)
                model, next_idx = _pick_next_in_pool(role_pool, start)
                _role_indexes[role] = next_idx

            if model is not None:
                response = await _try_model(model)
                if response is not None:
                    return response, model

                # First pick failed — try remaining models in the pool
                for _ in range(len(role_pool) - 1):
                    async with _router_lock:
                        start = _role_indexes.get(role, 0)
                        model, next_idx = _pick_next_in_pool(role_pool, start)
                        _role_indexes[role] = next_idx
                    if model is None:
                        break
                    response = await _try_model(model)
                    if response is not None:
                        return response, model

            logger.warning(
                f"All {len(role_pool)} role='{role}' model(s) exhausted "
                f"— falling through to general pool"
            )
        else:
            logger.warning(
                f"No models configured with role='{role}' "
                f"— falling through to general pool"
            )

    # ── Pass 1: General pool — locked round-robin (role == "") ──────────────────
    n_general = sum(1 for m in _models if not m.role and m.enabled)
    for _ in range(n_general):
        async with _router_lock:
            model, next_idx = _pick_next_available(_global_index)
            if model is None:
                break
            _global_index = next_idx

        response = await _try_model(model)
        if response is not None:
            return response, model

    # ── Pass 2: Paid pool — ultimate fallback (role == "paid_model") ────────────
    logger.warning("All general-pool models exhausted — switching to paid pool")
    for m in _models:
        if m.role == "paid_model" and m.enabled and m.is_available():
            response = await _try_model(m)
            if response is not None:
                return response, m

    raise RuntimeError(
        "All models exhausted including fallback. "
        "Check your API keys or wait for rate limits to reset."
    )


def get_all_model_status() -> list[dict]:
    return [{
        "name": m.name,
        "provider": m.provider,
        "model_id": m.model_id,
        "priority": m.priority,
        "role": m.role,
        "status": m.status,
        "rate_limit_reset_at": m.rate_limit_reset_at,
        "consecutive_errors": m.consecutive_errors,
        "total_calls": m.total_calls,
        "total_input_tokens": m.total_input_tokens,
        "total_output_tokens": m.total_output_tokens,
    } for m in _models]


def get_model_rate_limit_status() -> list[dict]:
    """
    Return per-model rate limit data for the dashboard:
      - Live header snapshot (from last API call)
      - Provider baselines (free / paid)
      - Model name, provider, status
    """
    result = []
    for m in _models:
        baselines = PROVIDER_BASELINES.get(m.provider, {})
        snap = m.rl_snapshot.to_dict() if m.rl_snapshot else {}
        result.append({
            "name": m.name,
            "model_id": m.model_id,
            "provider": m.provider,
            "priority": m.priority,
            "role": m.role,
            "status": m.status,
            "rate_limit_reset_at": m.rate_limit_reset_at,
            "total_calls": m.total_calls,
            "total_input_tokens": m.total_input_tokens,
            "total_output_tokens": m.total_output_tokens,
            "consecutive_errors": m.consecutive_errors,
            # Live snapshot from headers
            "live": snap,
            # Provider baselines
            "baselines": {
                "free": baselines.get("free"),
                "paid": baselines.get("paid"),
                "note": baselines.get("note", ""),
            },
        })
    return result


def reset_model(name: str) -> bool:
    for m in _models:
        if m.name == name:
            m.status = "available"
            m.rate_limit_reset_at = None
            m.consecutive_errors = 0
            return True
    return False


# Initial load
_load_models()
