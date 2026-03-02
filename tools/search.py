import httpx
from core.config_loader import get_apis
from core.rate_limiter import check_and_record

TOOL_DEFINITION = {
    "name": "web_search_tool",
    "description": (
        "Search the web for current information, news, facts, or documentation. "
        "Use for anything requiring up-to-date or real-time data. "
        "Do NOT use for questions answerable from memory or the system prompt. "
        "Numbers, statistics, scores, prices, dates, and measurements must be copied EXACTLY "
        "from tool results — never rounded, estimated, or paraphrased. "
        "Use the FIRST search result that clearly answers the question. Do not run a second "
        "search for the same thing — UNLESS the first result is vague or does not answer the query."
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
        },
        "max_results": {
            "type": "integer",
            "description": (
                "Optional. Number of results to return (1-10, default 6). "
                "Use 2-3 for quick single-fact lookups, 8-10 for deep research."
            )
        },
        "time_range": {
            "type": "string",
            "description": (
                "Optional. Limit results to a time range: 'day', 'week', 'month', 'year'. "
                "Use for queries where recency matters. Leave unset for timeless queries."
            ),
            "enum": ["day", "week", "month", "year"]
        },
        "days": {
            "type": "integer",
            "description": (
                "Optional. For news topic searches: how many days back to search (e.g. 3 = last 3 days). "
                "Use for tight recency control on breaking news queries."
            )
        },
        "include_domains": {
            "type": "array",
            "description": (
                "Optional. List of domains to restrict results to. "
                "Example: ['stackoverflow.com', 'docs.python.org'] for Python code questions."
            ),
            "items": {"type": "string"}
        },
        "exclude_domains": {
            "type": "array",
            "description": (
                "Optional. List of domains to exclude from results. "
                "Use to filter out low-quality or irrelevant sources."
            ),
            "items": {"type": "string"}
        }
    },
    "required": ["query"]
}

_NEWS_KEYWORDS = [
    "latest", "news", "today", "yesterday", "this week", "breaking",
    "update", "current", "recent", "now", "happened", "announced",
    "released", "launched", "just", "new ",
]


def _is_news_query(query: str) -> bool:
    q = query.lower()
    return any(kw in q for kw in _NEWS_KEYWORDS)


async def execute(
    query: str,
    max_results: int = 6,
    time_range: str = None,
    days: int = None,
    include_domains: list = None,
    exclude_domains: list = None,
) -> str:
    await check_and_record("tavily", wait=True)
    cfg = get_apis().get("search", {})
    api_key = cfg.get("tavily_api_key", "")

    if not api_key:
        return "Search tool error: tavily_api_key is not configured in apis.yaml."

    # Clamp max_results to valid range
    max_results = max(1, min(10, max_results))
    is_news = _is_news_query(query)

    payload = {
        "query": query,
        "search_depth": "advanced",
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": False,
        "chunks_per_source": 3,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    if is_news:
        payload["topic"] = "news"
        if days is not None:
            payload["days"] = days
    elif time_range:
        payload["time_range"] = time_range

    if include_domains:
        payload["include_domains"] = include_domains
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.tavily.com/search",
                headers=headers,
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

    lines = [f"Search results for: \"{query}\"\n"]

    for i, x in enumerate(results, 1):
        title = x.get("title", "No title")
        url = x.get("url", "")
        content = x.get("content", "").strip()[:600]
        published = x.get("published_date", "")

        lines.append(f"[{i}] {title}")
        if published:
            lines.append(f"    Date: {published}")
        lines.append(f"    URL: {url}")
        lines.append(f"    {content}")
        lines.append("")

    return "\n".join(lines)
