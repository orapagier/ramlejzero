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
from core.rate_limiter import check_and_record

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
    is_fallback: bool = False

    # Runtime state
    status: str = "available"
    rate_limit_reset_at: str | None = None
    consecutive_errors: int = 0
    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

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
# Provider call implementations
# ─────────────────────────────────────────────────────────────────────────────

async def _call_anthropic(model, messages, system, tools, max_tokens) -> UnifiedResponse:
    client = anthropic.AsyncAnthropic(api_key=model.api_key)
    kwargs: dict = dict(model=model.model_id, max_tokens=max_tokens, messages=messages, system=system)
    if tools:
        kwargs["tools"] = _to_anthropic_tools(tools)
    response = await client.messages.create(**kwargs)
    return _normalize_anthropic_response(response)


async def _call_openai_compat(model, messages, system, tools, max_tokens) -> UnifiedResponse:
    base_url = model.base_url or _PROVIDER_BASE_URLS.get(model.provider)
    api_key = model.api_key or "nokey"

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    oai_messages = _messages_to_openai(messages, system)

    kwargs: dict = dict(model=model.model_id, max_tokens=max_tokens, messages=oai_messages)
    if tools:
        kwargs["tools"] = _to_openai_tools(tools)
        kwargs["tool_choice"] = "auto"

    response = await client.chat.completions.create(**kwargs)
    return _normalize_openai_response(response)


async def _call_model(model, messages, system, tools, max_tokens) -> UnifiedResponse:
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
            is_fallback=m.get("fallback", False),
        ))

    _models.sort(key=lambda x: x.priority)
    providers = list({m.provider for m in _models})
    logger.info(f"Loaded {len(_models)} models | providers: {providers} | "
                f"priorities: {sorted({m.priority for m in _models})}")


def reload_models():
    global _global_index
    _global_index = 0
    _load_models()


# ─────────────────────────────────────────────────────────────────────────────
# Rotation state
# _router_lock ensures concurrent requests each pick a DIFFERENT model atomically
# ─────────────────────────────────────────────────────────────────────────────

_global_index: int = 0
_router_lock = asyncio.Lock()


def _pick_next_available(start: int, fallback_only: bool = False) -> tuple[ModelRecord | None, int]:
    """
    Walk the model list from start, return the first available model
    matching the fallback_only filter.
    Returns (model, next_index) or (None, start) if none available.
    """
    n = len(_models)
    for i in range(n):
        idx = (start + i) % n
        m = _models[idx]
        if m.is_fallback != fallback_only:
            continue
        if m.is_available():
            return m, (idx + 1) % n
    return None, start


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def call_llm(
    messages: list[dict],
    system: str = "",
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
) -> tuple[UnifiedResponse, ModelRecord]:
    """
    Two-pass model selection with locked round-robin.

    Pass 1 — Global round-robin across all non-fallback models.
      Lock is held ONLY during index selection (microseconds), not during the
      API call itself — so concurrent requests each get a different model
      and all their API calls run in parallel.

    Pass 2 — Fallback models, only when every free model is exhausted.

    Raises RuntimeError only when both passes fail completely.
    """
    global _global_index

    if not _models:
        _load_models()

    tools = tools or []

    async def _try_model(model: ModelRecord) -> UnifiedResponse | None:
        """Attempt one model call. Returns response or None on failure."""
        
        # Proactively check rate limits using the model's unique name
        if not await check_and_record(model.name, wait=False):
            return None

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

    # ── Pass 1: Free models, locked round-robin ──
    # Lock held only during index read+increment, NOT during API call.
    # This means concurrent requests pick different models, then call them in parallel.
    n_free = sum(1 for m in _models if not m.is_fallback and m.enabled)
    for _ in range(n_free):
        async with _router_lock:
            model, next_idx = _pick_next_available(_global_index, fallback_only=False)
            if model is None:
                break
            _global_index = next_idx  # advance index atomically before releasing lock

        response = await _try_model(model)  # API call outside lock — runs concurrently
        if response is not None:
            return response, model

    # ── Pass 2: Fallback models ──
    logger.warning("All free models exhausted — switching to fallback")
    fallback_idx = 0
    n_fallback = sum(1 for m in _models if m.is_fallback and m.enabled)
    for _ in range(n_fallback):
        async with _router_lock:
            model, fallback_idx = _pick_next_available(fallback_idx, fallback_only=True)
            if model is None:
                break

        response = await _try_model(model)
        if response is not None:
            return response, model

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
        "is_fallback": m.is_fallback,
        "status": m.status,
        "rate_limit_reset_at": m.rate_limit_reset_at,
        "consecutive_errors": m.consecutive_errors,
        "total_calls": m.total_calls,
        "total_input_tokens": m.total_input_tokens,
        "total_output_tokens": m.total_output_tokens,
    } for m in _models]


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
