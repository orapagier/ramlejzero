"""
tool_router.py — Binary Cascade Router
=======================================
Tier 1: Regex        (0 tokens)   — high-precision patterns, unambiguous terms only
Tier 2: Binary LLM   (~60 tokens) — fires only when regex is uncertain
Tier 3: Pass-through (0 extra)    — all tools go to main agent, let it decide

FOLLOW-UP HANDLING:
  "show me the nginx logs" after "check my server uptime" has no regex match,
  but Tier 2 receives:
    - Last 2 user messages for context
    - Last tool that was actually called (extracted from history)
  This lets the binary LLM correctly infer ssh_tool without needing a regex match.

REGEX DESIGN RULES:
  ✓ Multi-word phrases only — "tail the logs" not "logs"
  ✓ Only add if it maps to EXACTLY one tool with 100% certainty
  ✗ No single ambiguous words: "file", "open", "find", "check", "show", "get"
  ✗ Don't try to catch everything — a missed pattern costs ~60 tokens (Tier 2)
    but a false positive sends the wrong tool to the agent
"""

import re
import time
import logging
from core import model_router

logger = logging.getLogger("tool_router")


# ─────────────────────────────────────────────────────────────────────────────
# TIER 1 — HIGH-PRECISION REGEX
# ─────────────────────────────────────────────────────────────────────────────

_REGEX_MAP: dict[str, list[str]] = {

    "android_tool": [
        r"\bsend\s+(an?\s+)?sms\b",
        r"\btext\s+\w+\s+that\b",
        r"\bcall\s+(mom|dad|sis|bro|[a-z]{2,15})\s*$",
        r"\bmy\s+phone\s+battery\b",
        r"\bread\s+my\s+(phone\s+)?notifications\b",
        r"\bopen\s+\w+\s+on\s+my\s+phone\b",
        r"\bphone\s+(gps|location)\b",
    ],

    "gmail_tool": [
        r"\b(my\s+)?unread\s+(emails?|messages?)\b",
        r"\bcheck\s+my\s+(inbox|email)\b",
        r"\bsend\s+an?\s+email\b",
        r"\breply\s+to\s+(the\s+)?email\b",
        r"\bforward\s+(it|the\s+email)\b",
        r"\bgmail\b",
        r"\bdid\s+anyone\s+email\s+me\b",
    ],

    "google_calendar_tool": [
        r"\bmy\s+calendar\b",
        r"\bam\s+i\s+free\s+(today|tomorrow|this|on)\b",
        r"\bblock\s+off\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|morning|afternoon|evening)\b",
        r"\bschedule\s+(a\s+)?(meeting|call|appointment)\b",
        r"\bwhat\s+do\s+i\s+have\s+(today|tomorrow|this\s+week)\b",
        r"\bcancel\s+(the\s+)?(meeting|appointment|event)\b",
        r"\badd\s+(it\s+)?to\s+my\s+calendar\b",
        r"\bmove\s+my\s+\d+\s*(am|pm)\b",
    ],

    "google_drive_tool": [
        r"\bgoogle\s+drive\b",
        r"\bsave\s+(this\s+)?to\s+my\s+drive\b",
        r"\bupload\s+(to\s+)?drive\b",
        r"\bmy\s+drive\b",
    ],

    "onedrive_tool": [
        r"\bonedrive\b",
        r"\bmicrosoft\s+(storage|cloud|drive)\b",
        r"\bsave\s+(to\s+)?onedrive\b",
        r"\b\.xlsx\b",
        r"\bexcel\s+file\b",
        r"\bword\s+document\b",
        r"\b\.docx\b",
    ],

    "ssh_tool": [
        r"\bmy\s+(linux\s+)?server\b",
        r"\bssh\s+(into|to)\b",
        r"\bdocker\s+(ps|logs?|container|compose|run|exec)\b",
        r"\btail\s+(the\s+)?logs?\b",
        r"\bwhat'?s\s+running\s+on\s+port\b",
        r"\bdisk\s+(space|usage)\b",
        r"\bfree\s+up\s+(disk|space)\b",
        r"\b(restart|start|stop)\s+(nginx|apache|the\s+server|the\s+service)\b",
        r"\bserver\s+(uptime|cpu|memory|load)\b",
        r"\bcheck\s+if\s+the\s+site\s+is\s+up\b",
        r"\bsystemctl\b",
    ],

    "windows_tool": [
        r"\bmy\s+(windows\s+)?(pc|desktop|machine)\b",
        r"\btake\s+a\s+screenshot\b",
        r"\bwhat\s+apps?\s+are\s+(open|running)\b",
        r"\bclose\s+that\s+window\b",
        r"\bpaste\s+(this\s+)?into\b",
        r"\bopen\s+\w+\s+on\s+(my\s+)?(windows|pc|desktop)\b",
        r"\bping\s+my\s+(windows|pc|machine)\b",
    ],

    "web_search_tool": [
        r"\blook\s+it\s+up\b",
        r"\bsearch\s+(the\s+web\s+for|online\s+for)\b",
        r"\bwhat'?s\s+the\s+latest\s+(news\s+on|on)\b",
        r"\bcurrent\s+(price|news|score|status)\s+of\b",
        r"\bfind\s+out\s+(who|what|when|how|if)\b",
    ],

    "agent_memory_tool": [
        # Explicit memory commands
        r"\bdo\s+you\s+remember\b",
        r"\bwhat\s+did\s+i\s+tell\s+you\s+(about|last)\b",
        r"\bkeep\s+in\s+mind\b",
        r"\bremember\s+that\b",
        r"\bmy\s+preference(s)?\b",
        r"\bsave\s+this\s+(to\s+)?(memory|your\s+memory)\b",
        # Personal questions about the user — need memory to answer
        r"\bwhat\s+(is|are|was|were)\s+my\b",
        r"\bwhat\s+do\s+i\s+(like|dislike|prefer|want|need|use|have|own)\b",
        r"\bwhat\s+am\s+i\b",
        r"\bwho\s+am\s+i\b",
        r"\bmy\s+(name|age|job|work|address|birthday|setup|server|phone)\b",
        r"\btell\s+me\s+about\s+my(self)?\b",
        r"\bwhat\s+do\s+you\s+know\s+about\s+me\b",
    ],
}

_COMPILED: dict[str, list[re.Pattern]] = {
    tool: [re.compile(p, re.IGNORECASE) for p in patterns]
    for tool, patterns in _REGEX_MAP.items()
}

# Pure conversational — skip tool routing entirely, zero cost
_CONVERSATIONAL = re.compile(
    r"^("
    # Greetings and acknowledgements
    r"hi+|hello|hey|thanks|thank\s+you|ok(ay)?|sure|yes|no|nope|yep|bye|goodbye|"
    r"good\s+(morning|evening|night|day)|how\s+are\s+you|what'?s\s+up|got\s+it|"
    r"sounds\s+good|perfect|great|cool|nice|awesome|lol|haha|hmm+|alright|"
    r"nevermind|never\s+mind|nvm|noted|understood|makes\s+sense|"
    # Date/time questions the system prompt can answer (has current date/time)
    r"what\s+(day|date|time)\s+is\s+(it|today|now)|what'?s\s+today'?s\s+date|"
    r"how\s+about\s+(tomorrow|yesterday|today|tonight|next\s+\w+)|"
    r"what\s+day\s+is\s+(tomorrow|yesterday|next\s+\w+)|"
    r"what\s+time\s+is\s+it(\s+now)?|what'?s\s+the\s+(date|time|day)(\s+today)?|"
    # Bare follow-ups — let main LLM handle with full history context
    r"i\s+see|what\s+do\s+i\s+need\s+to\s+do(\s+now)?|how\s+do\s+i\s+(do|fix)\s+that|"
    r"can\s+you\s+explain(\s+more)?|tell\s+me\s+more|what\s+does\s+that\s+mean|"
    r"(yes\s+)?please(\s+do\s+(it|that))?|go\s+ahead|do\s+it|"
    r"what\s+should\s+i\s+do(\s+next)?|what\s+happened"
    r")\W*$",
    re.IGNORECASE
)


def _tier1_regex(message: str) -> tuple[list[str], bool]:
    """Returns (matched_tools, is_confident). Confident = 1-3 unambiguous matches."""
    matched = [
        tool for tool, patterns in _COMPILED.items()
        if any(p.search(message) for p in patterns)
    ]
    return matched, 1 <= len(matched) <= 3


# ─────────────────────────────────────────────────────────────────────────────
# CONTEXT EXTRACTION — for follow-up resolution
# ─────────────────────────────────────────────────────────────────────────────

def _extract_last_tool_used(history: list | None) -> str | None:
    """
    Walk history backwards to find the last tool_use block.
    Returns the tool name, e.g. "ssh_tool".
    This is the key to follow-up accuracy:
      User: "check my server uptime"  → ssh_tool (regex match)
      User: "now show the nginx logs" → no regex match
      But history shows last tool = ssh_tool → Tier 2 can infer correctly
    """
    if not history:
        return None
    for msg in reversed(history):
        if msg["role"] != "assistant":
            continue
        content = msg["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            # Works with both Anthropic SDK objects and raw dicts
            block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
            if block_type == "tool_use":
                return getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None)
    return None


def _extract_last_user_messages(history: list | None, n: int = 2) -> list[str]:
    """Extract the last N user messages as plain strings for context."""
    if not history:
        return []
    messages = []
    for msg in reversed(history):
        if msg["role"] == "user":
            content = msg["content"]
            if isinstance(content, str):
                messages.append(content[:120])
            elif isinstance(content, list):
                # Skip pure tool result messages
                text = " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                if text:
                    messages.append(text[:120])
            if len(messages) >= n:
                break
    return list(reversed(messages))  # chronological order


# ─────────────────────────────────────────────────────────────────────────────
# TIER 2 — BINARY LLM (~60 tokens in, ~15 out)
# ─────────────────────────────────────────────────────────────────────────────

_BINARY_SYSTEM = (
    "Tool selector. Reply ONLY with comma-separated tool names or NONE. "
    "No explanation, no formatting, exact names only."
)


def _build_binary_prompt(
    message: str,
    tool_defs: list[dict],
    history: list | None,
) -> str:
    lines = ["Tools:"]
    for t in tool_defs:
        short = t.get("description", "").split(".")[0][:70]
        lines.append(f"- {t['name']}: {short}")

    # Prior user messages (last 2) for follow-up awareness
    prior_msgs = _extract_last_user_messages(history, n=2)
    if prior_msgs:
        lines.append(f"Prior messages: {' → '.join(prior_msgs)}")

    # Last tool used — critical for follow-up routing
    last_tool = _extract_last_tool_used(history)
    if last_tool:
        lines.append(f"Last tool used: {last_tool}")

    lines.append(f"Current request: {message}")
    lines.append("Tools needed:")
    return "\n".join(lines)


async def _tier2_binary(
    message: str,
    tool_defs: list[dict],
    history: list | None,
) -> tuple[list[str], dict]:
    prompt = _build_binary_prompt(message, tool_defs, history)
    all_names = {t["name"] for t in tool_defs}
    t0 = time.monotonic()

    try:
        response, model_record = await model_router.call_llm(
            messages=[{"role": "user", "content": prompt}],
            system=_BINARY_SYSTEM,
            tools=[],
            max_tokens=40,
        )
        raw = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        duration_ms = (time.monotonic() - t0) * 1000

        telem = {
            "model": model_record.name,
            "method": "binary_llm",
            "in_tokens": response.usage.input_tokens,
            "out_tokens": response.usage.output_tokens,
            "duration_ms": duration_ms,
        }

        if not raw or raw.upper() == "NONE":
            return [], telem

        selected = [n.strip() for n in raw.split(",") if n.strip() in all_names]
        return selected, telem

    except Exception as e:
        logger.warning(f"Binary LLM router failed: {e}")
        return [], {
            "model": "none", "method": "binary_llm_failed",
            "in_tokens": 0, "out_tokens": 0,
            "duration_ms": (time.monotonic() - t0) * 1000,
        }


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

async def filter_tools(
    user_message: str,
    all_tool_definitions: list[dict],
    conversation_history: list | None = None,
) -> tuple[list[dict], dict]:
    """
    Returns (filtered_tool_definitions, telemetry_dict).

    Flow:
      Conversational shortcut → [] (0 tokens)
      Tier 1 regex confident  → matched tools (0 tokens)
      Tier 2 binary LLM       → ~60 tokens, fires on ambiguity/no-match
      Tier 3 pass-through     → all tools → main agent decides (0 extra tokens)

    Follow-up handling:
      Tier 2 receives last 2 user messages + last tool used from history.
      "now show the nginx access log" after an ssh session correctly resolves
      to ssh_tool even with zero regex match on the follow-up message.
    """
    msg = user_message.strip()
    name_map = {t["name"]: t for t in all_tool_definitions}
    zero = {"method": "regex", "model": "none", "in_tokens": 0, "out_tokens": 0, "duration_ms": 0}

    # Conversational shortcut
    if _CONVERSATIONAL.match(msg):
        logger.info("Router: conversational → no tools")
        return [], {**zero, "method": "conversational"}

    # Tier 1: Regex
    regex_matches, confident = _tier1_regex(msg)
    if confident:
        selected = [name_map[n] for n in regex_matches if n in name_map]
        logger.info(f"Router Tier1/regex → {regex_matches}")
        return selected, zero

    # Tier 2: Binary LLM
    # If regex gave partial (non-confident) matches, narrow the search space.
    # Otherwise pass all tools so the LLM can consider everything.
    candidates = (
        [name_map[n] for n in regex_matches if n in name_map]
        if regex_matches else all_tool_definitions
    )

    llm_matches, telem = await _tier2_binary(msg, candidates, conversation_history)
    if llm_matches:
        selected = [name_map[n] for n in llm_matches if n in name_map]
        logger.info(
            f"Router Tier2/binary → {llm_matches} | "
            f"last_tool={_extract_last_tool_used(conversation_history)} | "
            f"{telem['in_tokens']} tokens"
        )
        return selected, telem

    # Tier 3: Pass-through
    logger.info("Router Tier3/passthrough → all tools")
    return all_tool_definitions, {**telem, "method": "passthrough"}
