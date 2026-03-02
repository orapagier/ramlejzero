import httpx
from core.config_loader import get_apis
from core.rate_limiter import check_and_record

TOOL_DEFINITION = {
    "name": "web_search_tool",
    "description": (
        "Search the web for current information, news, facts, or documentation. "
        "Use for anything requiring up-to-date or real-time data. "
        "Do NOT use for questions answerable from memory or the system prompt."
        "Numbers, statistics, scores, prices, dates, and measurements must be copied EXACTLY from tool results — never rounded, estimated, or paraphrased."
        "When reporting factual figures from search results, prefer quoting the source directly over summarizing."
        "Use the FIRST search result that clearly answers the question. Do not run a second search for the same thing — UNLESS the first result is vague or does not answer the user's query"
    ),
    "examples": [
        "look it up",
        "find out who made this",
        "what's the latest on that",
    ],
    "parameters": {
        "query": {
            "type": "string",
            "description": "The search query. Be specific — include names, dates, or version numbers when relevant."
        }
    },
    "required": ["query"]
}

# Keywords that indicate the query is about current news/events.
# These trigger topic="news" which prioritizes fresh, recent sources.
_NEWS_KEYWORDS = [
    "latest", "news", "today", "yesterday", "this week", "breaking",
    "update", "current", "recent", "now", "happened", "announced",
    "released", "launched", "just", "new ",
]


def _is_news_query(query: str) -> bool:
    q = query.lower()
    return any(kw in q for kw in _NEWS_KEYWORDS)


async def execute(query: str) -> str:
    await check_and_record("tavily", wait=True)
    cfg = get_apis().get("search", {})
    api_key = cfg.get("tavily_api_key", "")

    if not api_key:
        return "Search tool error: tavily_api_key is not configured in apis.yaml."

    is_news = _is_news_query(query)

    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "advanced",   # full page content vs shallow scrape
        "max_results": 6,
        "include_answer": False,       # skip Tavily's AI summary — it can be wrong
        "include_raw_content": False,  # structured content is cleaner
        "chunks_per_source": 3,        # get multiple passages per source
    }

    # Use news topic for time-sensitive queries — prioritizes fresh sources
    if is_news:
        payload["topic"] = "news"

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.tavily.com/search",
                json=payload,
                timeout=40
            )

        if r.status_code != 200:
            return f"Search failed (HTTP {r.status_code}): {r.text[:200]}"

        data = r.json()

    except httpx.TimeoutException:
        return "Search failed: request timed out. Try a simpler query."
    except Exception as e:
        return f"Search failed: {str(e)}"

    results = data.get("results", [])
    if not results:
        return "No results found."

    lines = []

    # Show query context so the agent knows what it searched for
    lines.append(f"Search results for: \"{query}\"\n")

    for i, x in enumerate(results, 1):
        title = x.get("title", "No title")
        url = x.get("url", "")
        # Use up to 600 chars — enough for the agent to reason accurately
        content = x.get("content", "").strip()[:600]
        published = x.get("published_date", "")
        score = x.get("score", 0)

        lines.append(f"[{i}] {title}")
        if published:
            lines.append(f"    Date: {published}")
        lines.append(f"    URL: {url}")
        lines.append(f"    {content}")
        lines.append("")  # blank line between results

    return "\n".join(lines)
