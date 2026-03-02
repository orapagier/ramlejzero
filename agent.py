"""
agent.py — Core agent loop.
Add tools by dropping .py files in tools/.
Add/change AI models in config/models.yaml.
"""
import os
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

    prompt_path = os.path.join(os.path.dirname(__file__), "config", "system_prompt.txt")
    with open(prompt_path, "r", encoding="utf-8") as f:
        template = f.read()

    return template.format_map({
        "now": now,
        "location": location,
        "google_account": google_account,
        "timezone": tz_name
    })


def _count_exchanges(history: list) -> int:
    return sum(1 for m in history if m["role"] == "user")


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

    # ── Tool routing ──
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

    # ── Build system prompt ──
    base_prompt = _build_system_prompt()

    if not tool_definitions:
        system_prompt = base_prompt
    else:
        tool_list_str = ", ".join(t["name"] for t in tool_definitions)
        directive = (
            f"### ACTIVE TOOLS: {tool_list_str} ###\n"
            "Use the appropriate tool to complete the task. "
            "Call the tool immediately without explanation. "
            "If the message is purely conversational, respond directly.\n\n"
        )
        system_prompt = directive + base_prompt

    # ── Agent loop ──
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
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_name = block.name
                tool_input = block.input
                tc_start = time.monotonic()
                logger.info(f"Tool call: {tool_name} | params: {list(tool_input.keys())}")

                try:
                    tool_result = await _execute_tool(tool_name, tool_input)
                    if tool_result.file_bytes:
                        file_bytes = tool_result.file_bytes
                        filename = tool_result.filename
                    result_text = tool_result.text
                    success = tool_result.success
                    error = tool_result.error
                except Exception as e:
                    result_text = f"Tool error: {str(e)}"
                    success = False
                    error = str(e)
                    logger.error(f"Tool {tool_name} raised: {e}", exc_info=True)

                tc_duration = (time.monotonic() - tc_start) * 1000
                tool_calls_log.append(ToolCallLog(
                    tool_name=tool_name,
                    input_params=tool_input,
                    result_text=result_text[:500],
                    success=success,
                    duration_ms=tc_duration,
                    error=error if not success else None
                ))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text
                })

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


async def _execute_tool(tool_name: str, tool_input: dict) -> ToolResult:
    if tool_name not in TOOLS:
        return ToolResult.fail(f"Tool '{tool_name}' not found")
    tool_data = TOOLS[tool_name]
    module = importlib.import_module(f"tools.{tool_data['module']}")
    try:
        raw = await module.execute(**tool_input)
        if isinstance(raw, tuple):
            text, fb, fn = raw
            return ToolResult.ok(text=str(text), file_bytes=fb, filename=fn)
        return ToolResult.ok(text=str(raw))
    except Exception as e:
        return ToolResult.fail(str(e))
