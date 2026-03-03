"""
agent.py — Core agent loop.
Add tools by dropping .py files in tools/.
Add/change AI models in config/models.yaml.

v2 improvements:
  - Prompt template cached at startup (no disk I/O per call)
  - Parallel tool execution  : multiple tool_use blocks run concurrently
  - Task planner             : complex multi-step tasks get an upfront plan
  - Tool retry               : transient failures retry once before surfacing
  - Structured tool results  : status/duration metadata helps the model reason
"""
import asyncio
import os
import re
import time
from datetime import datetime
from core.schemas import AgentResponse, ToolCallLog, ToolResult
from core import model_router
from core.logger import get_logger, log_agent_run
from core.schemas import AgentRunLog
from core.config_loader import get_settings
from core.tool_router import filter_tools
from tools import get_tool_definitions, TOOLS
import importlib
import pytz

logger = get_logger("agent")

# ── System prompt template cache ──────────────────────────────────────────────
# Read once at startup; format_map() still runs per call to inject time/location.
_PROMPT_TEMPLATE: str | None = None


def _get_prompt_template() -> str:
    global _PROMPT_TEMPLATE
    if _PROMPT_TEMPLATE is None:
        prompt_path = os.path.join(os.path.dirname(__file__), "config", "system_prompt.txt")
        with open(prompt_path, "r", encoding="utf-8") as f:
            _PROMPT_TEMPLATE = f.read()
    return _PROMPT_TEMPLATE


def _history_config() -> tuple[int, int]:
    agent_cfg = get_settings().get("agent", {})
    max_ex = agent_cfg.get("history_max_exchanges", 6)
    summarize_at = agent_cfg.get("history_summarize_at", 8)
    if summarize_at <= max_ex:
        summarize_at = max_ex + 2
    return max_ex, summarize_at


def _build_system_prompt() -> str:
    settings = get_settings()
    agent_cfg = settings.get("agent", {})
    tz_name = agent_cfg.get("timezone", "Asia/Manila")
    location = agent_cfg.get("location", "")
    google_account = agent_cfg.get("google_account", "")

    tz = pytz.timezone(tz_name)
    now = datetime.now(tz).strftime("%A, %B %d, %Y, %I:%M %p")

    return _get_prompt_template().format_map({
        "now": now,
        "location": location,
        "google_account": google_account,
        "timezone": tz_name
    })


def _count_exchanges(history: list) -> int:
    return sum(1 for m in history if m["role"] == "user")


# ── History summarisation ─────────────────────────────────────────────────────

async def _summarize_history(history: list) -> str:
    if not history:
        return ""

    lines = []
    for msg in history:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str) and content:
            lines.append(f"{role.upper()}: {content[:300]}")
        elif isinstance(content, list):
            text = " ".join(
                (b.get("text", "") if isinstance(b, dict) else getattr(b, "text", ""))
                for b in content
                if (isinstance(b, dict) and b.get("type") == "text") or
                   (hasattr(b, "type") and b.type == "text")
            )
            if text:
                lines.append(f"{role.upper()}: {text[:300]}")

    if not lines:
        return ""

    transcript = "\n".join(lines)
    try:
        response, _ = await model_router.call_llm(
            messages=[{
                "role": "user",
                "content": (
                    f"Summarize this conversation in 3-5 sentences. "
                    f"Keep names, values, decisions, and key context:\n\n{transcript}"
                )
            }],
            system="Concise conversation summarizer. Preserve facts and decisions.",
            tools=[],
            max_tokens=200,
        )
        return "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    except Exception as e:
        logger.warning(f"History summarization failed: {e}")
        return " | ".join(lines[:3])


async def trim_history(history: list) -> list:
    max_exchanges, summarize_at = _history_config()

    if _count_exchanges(history) <= summarize_at:
        return history

    keep_from = 0
    exchange_count = 0
    for i in range(len(history) - 1, -1, -1):
        if history[i]["role"] == "user":
            exchange_count += 1
            if exchange_count >= max_exchanges:
                keep_from = i
                break

    old_history = history[:keep_from]
    recent_history = history[keep_from:]

    if not old_history:
        return recent_history

    summary = await _summarize_history(old_history)
    if summary:
        logger.info(f"History trimmed: {len(old_history)} msgs → summary + {len(recent_history)} recent msgs")
        return [
            {"role": "user", "content": f"[Earlier conversation summary: {summary}]"},
            {"role": "assistant", "content": "Understood, I have context from our earlier conversation."},
        ] + recent_history

    return recent_history


# ── Task complexity & planning ────────────────────────────────────────────────

_MULTISTEP_RE = re.compile(
    r"\b(then|and\s+then|after\s+that|afterwards|next\s*[,;]?"
    r"|and\s+also|followed\s+by|once\s+(that'?s?\s+)?done"
    r"|first\s*[,;]|second\s*[,;]|third\s*[,;]|finally\s*[,;]?|lastly"
    r"|before\s+that|additionally|as\s+well\s+as)\b",
    re.IGNORECASE,
)

_PLAN_MIN_WORDS = 12


def _is_complex_task(message: str, tool_definitions: list[dict]) -> bool:
    """
    True when the task is likely multi-step and benefits from an upfront plan.
    Requires: long enough message + step connectors + at least 2 available tools.
    """
    if len(message.split()) < _PLAN_MIN_WORDS:
        return False
    if len(tool_definitions) < 2:
        return False
    return bool(_MULTISTEP_RE.search(message))


_PLANNER_SYSTEM = (
    "You are a task planner. Given a user request and a list of available tools, "
    "output a concise numbered plan (max 6 steps). "
    "Each step: which tool to call and what to do. "
    "Be brief — one sentence per step. No preamble, no explanation outside the plan."
)


async def _generate_plan(message: str, tool_definitions: list[dict]) -> str | None:
    """
    Lightweight LLM pass that produces a numbered execution plan.
    Injected into the system prompt so the agent sequences tools correctly.
    Best-effort — returns None on failure and never blocks execution.
    """
    tool_names = ", ".join(t["name"] for t in tool_definitions)
    prompt = (
        f"Available tools: {tool_names}\n\n"
        f"User request: {message}\n\n"
        "Write a step-by-step plan to complete this request using the tools above."
    )
    try:
        response, _ = await model_router.call_llm(
            messages=[{"role": "user", "content": prompt}],
            system=_PLANNER_SYSTEM,
            tools=[],
            max_tokens=250,
        )
        plan = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        logger.info(f"Planner output:\n{plan}")
        return plan
    except Exception as e:
        logger.warning(f"Planner failed (non-fatal): {e}")
        return None


# ── Tool execution ────────────────────────────────────────────────────────────

_TOOL_RETRY_KEYWORDS = (
    "timeout", "connection", "network", "temporarily", "retry", "503", "502",
)


def _is_retryable(error: str) -> bool:
    err_lower = error.lower()
    return any(k in err_lower for k in _TOOL_RETRY_KEYWORDS)


async def _execute_tool(tool_name: str, tool_input: dict) -> ToolResult:
    """Execute a single tool, with one automatic retry on transient failures."""
    if tool_name not in TOOLS:
        return ToolResult.fail(f"Tool '{tool_name}' not found")

    tool_data = TOOLS[tool_name]
    module = importlib.import_module(f"tools.{tool_data['module']}")

    async def _run_once() -> ToolResult:
        try:
            raw = await module.execute(**tool_input)
            if isinstance(raw, tuple):
                text, fb, fn = raw
                return ToolResult.ok(text=str(text), file_bytes=fb, filename=fn)
            return ToolResult.ok(text=str(raw))
        except Exception as e:
            return ToolResult.fail(str(e))

    result = await _run_once()

    # One retry on transient errors (timeouts, 502/503s, network blips)
    if not result.success and _is_retryable(result.error or ""):
        logger.info(f"Retrying {tool_name} after transient error: {result.error}")
        await asyncio.sleep(1.5)
        result = await _run_once()
        if not result.success:
            logger.warning(f"Retry failed for {tool_name}: {result.error}")

    return result


def _format_tool_result(tool_name: str, result: ToolResult, duration_ms: float) -> str:
    """
    Structured result string so the model can reason about success/failure
    and execution metadata — not just a raw text dump.
    """
    status = "success" if result.success else "error"
    parts = [f"[tool:{tool_name}] [status:{status}] [duration:{duration_ms:.0f}ms]"]
    if result.success:
        parts.append(result.text)
    else:
        parts.append(f"Error: {result.error}")
        # Surface partial output if the tool returned something before failing
        if result.text and result.text != result.error:
            parts.append(f"Partial output: {result.text}")
    return "\n".join(parts)


async def _execute_tools_parallel(
    tool_blocks: list,
) -> tuple[list[dict], list[ToolCallLog], bytes | None, str | None]:
    """
    Execute all tool_use blocks from a single model turn concurrently.
    Independent calls (e.g. "search the web AND check my calendar") run in
    parallel, cutting wall-clock time proportionally.

    Returns (tool_results_for_messages, tool_calls_log, file_bytes, filename).
    """
    async def _handle_one(block):
        tool_name = block.name
        tool_input = block.input
        tc_start = time.monotonic()
        logger.info(f"Tool call (parallel): {tool_name} | params: {list(tool_input.keys())}")

        result = await _execute_tool(tool_name, tool_input)
        tc_duration = (time.monotonic() - tc_start) * 1000

        log_entry = ToolCallLog(
            tool_name=tool_name,
            input_params=tool_input,
            result_text=result.text[:500] if result.text else "",
            success=result.success,
            duration_ms=tc_duration,
            error=result.error if not result.success else None,
        )
        result_msg = {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": _format_tool_result(tool_name, result, tc_duration),
        }
        return result_msg, log_entry, result

    outcomes = await asyncio.gather(
        *[_handle_one(b) for b in tool_blocks],
        return_exceptions=True,
    )

    tool_results = []
    tool_call_logs = []
    file_bytes = None
    filename = None

    for i, outcome in enumerate(outcomes):
        if isinstance(outcome, Exception):
            block = tool_blocks[i]
            logger.error(f"Unexpected error in parallel tool {block.name}: {outcome}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"[tool:{block.name}] [status:error]\nError: {outcome}",
            })
            tool_call_logs.append(ToolCallLog(
                tool_name=block.name,
                input_params=block.input,
                result_text="",
                success=False,
                duration_ms=0,
                error=str(outcome),
            ))
        else:
            result_msg, log_entry, result = outcome
            tool_results.append(result_msg)
            tool_call_logs.append(log_entry)
            if result.file_bytes:
                file_bytes = result.file_bytes
                filename = result.filename

    return tool_results, tool_call_logs, file_bytes, filename


# ── Main agent loop ───────────────────────────────────────────────────────────

async def run(
    user_message: str,
    user_id: int,
    conversation_history: list | None = None,
) -> AgentResponse:
    """
    Run the agent loop.
    Returns AgentResponse with full stats and optional file attachment.
    """
    settings = get_settings()
    max_iterations = settings.get("agent", {}).get("max_iterations", 10)
    _, summarize_at = _history_config()

    history = list(conversation_history or [])
    if _count_exchanges(history) > summarize_at:
        history = await trim_history(history)

    messages = history + [{"role": "user", "content": user_message}]

    total_input_tokens = 0
    total_output_tokens = 0
    tool_calls_log: list[ToolCallLog] = []
    model_used = "unknown"
    file_bytes = None
    filename = None
    start_time = time.monotonic()

    # ── Tool routing ──────────────────────────────────────────────────────────
    all_tool_definitions = get_tool_definitions()
    tool_definitions, router_telem = await filter_tools(
        user_message, all_tool_definitions, conversation_history
    )

    from core.logger import log_router_run
    log_router_run(
        user_id=user_id,
        user_message=user_message,
        selected_tools=[t["name"] for t in tool_definitions],
        model=router_telem["model"],
        in_tokens=router_telem["in_tokens"],
        out_tokens=router_telem["out_tokens"],
        duration_ms=router_telem["duration_ms"]
    )

    logger.info(
        f"Tool routing | user_id={user_id} | method={router_telem['method']} | "
        f"selected {len(tool_definitions)}/{len(all_tool_definitions)}: "
        f"{[t['name'] for t in tool_definitions]}"
    )

    # ── Task planner (multi-step tasks only) ──────────────────────────────────
    plan_text: str | None = None
    if tool_definitions and _is_complex_task(user_message, tool_definitions):
        logger.info("Complex multi-step task detected — generating plan")
        plan_text = await _generate_plan(user_message, tool_definitions)

    # ── Build system prompt ───────────────────────────────────────────────────
    base_prompt = _build_system_prompt()

    if not tool_definitions:
        system_prompt = base_prompt
    else:
        tool_list_str = ", ".join(t["name"] for t in tool_definitions)
        directive_parts = [
            f"### ACTIVE TOOLS: {tool_list_str} ###",
            "Use the appropriate tool to complete the task. "
            "Call the tool immediately without explanation. "
            "You may call multiple tools in a single turn when they are independent. "
            "If the message is purely conversational, respond directly.",
        ]
        if plan_text:
            directive_parts.append(
                f"\n### EXECUTION PLAN ###\n{plan_text}\n"
                "Follow this plan step by step. You may parallelise independent steps."
            )
        directive_parts.append("")
        system_prompt = "\n".join(directive_parts) + "\n" + base_prompt

    # ── Agent loop ────────────────────────────────────────────────────────────
    for iteration in range(max_iterations):
        logger.info(f"Iteration {iteration + 1}/{max_iterations} | user_id={user_id}")

        try:
            response, model_record = await model_router.call_llm(
                messages=messages,
                system=system_prompt,
                tools=tool_definitions,
            )
            model_used = model_record.name
        except RuntimeError as e:
            duration = (time.monotonic() - start_time) * 1000
            return AgentResponse(
                text=str(e),
                model_used="none",
                iterations=iteration + 1,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                duration_ms=duration,
                success=False,
                error=str(e)
            )

        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        logger.info(
            f"Model: {model_used} | stop: {response.stop_reason} | "
            f"tokens in={response.usage.input_tokens} out={response.usage.output_tokens}"
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            text = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            duration = (time.monotonic() - start_time) * 1000
            result = AgentResponse(
                text=text,
                file_bytes=file_bytes,
                filename=filename,
                model_used=model_used,
                iterations=iteration + 1,
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                duration_ms=duration,
                tool_calls=tool_calls_log,
                success=True
            )
            log_agent_run(user_id, AgentRunLog(
                user_id=user_id,
                user_message=user_message,
                response=result
            ))
            return result

        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]

            if not tool_blocks:
                logger.warning("stop_reason=tool_use but no tool_use blocks found")
                continue

            logger.info(
                f"Parallel tool execution: {len(tool_blocks)} tool(s) → "
                f"{[b.name for b in tool_blocks]}"
            )

            tool_results, turn_logs, turn_file_bytes, turn_filename = (
                await _execute_tools_parallel(tool_blocks)
            )

            tool_calls_log.extend(turn_logs)
            if turn_file_bytes:
                file_bytes = turn_file_bytes
                filename = turn_filename

            messages.append({"role": "user", "content": tool_results})

    duration = (time.monotonic() - start_time) * 1000
    return AgentResponse(
        text="I reached the maximum number of steps. Please try breaking the task into smaller parts.",
        model_used=model_used,
        iterations=max_iterations,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        duration_ms=duration,
        tool_calls=tool_calls_log,
        success=False,
        error="max_iterations_reached"
    )
