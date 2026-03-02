import asyncio
from datetime import datetime
from core.schemas import RateLimitState
from core.config_loader import get_settings, get_models_config
from core.logger import get_logger

logger = get_logger("rate_limiter")

# Global rate limit state per API
_states: dict[str, RateLimitState] = {}
_lock = asyncio.Lock()


def _get_state(api_name: str) -> RateLimitState:
    if not _states:
        # Seed all configured limits from settings
        settings = get_settings()
        limits = settings.get("rate_limits", {})
        for name, cfg in limits.items():
            max_calls = cfg.get("calls_per_minute", 60)
            _states[name] = RateLimitState(
                api_name=name,
                max_calls_per_minute=max_calls
            )
        
        # Also seed all enabled models from models.yaml
        models_cfg = get_models_config()
        for m in models_cfg.get("models", []):
            if not m.get("enabled", True):
                continue
            name = m.get("name")
            if name and name not in _states:
                _states[name] = RateLimitState(
                    api_name=name,
                    max_calls_per_minute=limits.get(name, {}).get("calls_per_minute", 60)
                )

    if api_name not in _states:
        # Fallback for dynamic API names or ones missing from seed
        settings = get_settings()
        limits = settings.get("rate_limits", {})
        api_cfg = limits.get(api_name, {})
        max_calls = api_cfg.get("calls_per_minute", 60)
        _states[api_name] = RateLimitState(
            api_name=api_name,
            max_calls_per_minute=max_calls
        )
    return _states[api_name]


async def check_and_record(api_name: str, wait: bool = True) -> bool:
    """
    Check if an API call is allowed under rate limits.
    If wait=True, blocks until the call is allowed.
    If wait=False, returns False immediately if rate limited.
    """
    async with _lock:
        state = _get_state(api_name)

        if state.is_allowed():
            state.record_call()
            return True

        if not wait:
            state.record_blocked()
            logger.warning(
                f"Rate limit hit | {api_name} | "
                f"{state.calls_this_minute}/{state.max_calls_per_minute} calls/min | "
                f"blocked total: {state.total_blocked}"
            )
            return False

        # Wait until window resets
        wait_seconds = state.seconds_until_reset + 0.1
        logger.info(f"Rate limit | {api_name} | waiting {wait_seconds:.1f}s")

    await asyncio.sleep(wait_seconds)

    async with _lock:
        state = _get_state(api_name)
        state.record_call()
        return True


def get_all_states() -> dict[str, dict]:
    """Return current rate limit states for all APIs — used by Web UI."""
    if not _states:
        _get_state("seed_trigger")
    return {
        name: {
            "calls_this_minute": s.calls_this_minute,
            "max_per_minute": s.max_calls_per_minute,
            "total_calls": s.total_calls,
            "total_blocked": s.total_blocked,
            "seconds_until_reset": round(s.seconds_until_reset, 1)
        }
        for name, s in _states.items()
    }


def reload_limits():
    """Refresh max_calls_per_minute from config for all states."""
    settings = get_settings()
    limits = settings.get("rate_limits", {})
    
    # Update all existing manually defined limits
    for name, cfg in limits.items():
        max_calls = cfg.get("calls_per_minute", 60)
        if name in _states:
            _states[name].max_calls_per_minute = max_calls
        else:
            _states[name] = RateLimitState(api_name=name, max_calls_per_minute=max_calls)
            
    # Update model name limits
    models_cfg = get_models_config()
    for m in models_cfg.get("models", []):
        name = m.get("name")
        if not name or not m.get("enabled", True):
            continue
        # Use specific model override if it exists, else default 60
        max_calls = limits.get(name, {}).get("calls_per_minute", 60)
        if name in _states:
            _states[name].max_calls_per_minute = max_calls
        else:
            _states[name] = RateLimitState(api_name=name, max_calls_per_minute=max_calls)
