import yaml
import os
from functools import lru_cache

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")


def _load(filename: str) -> dict:
    path = os.path.join(CONFIG_DIR, filename)
    with open(path, "r") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=None)
def get_settings() -> dict:
    return _load("settings.yaml")


@lru_cache(maxsize=None)
def get_apis() -> dict:
    return _load("apis.yaml")


@lru_cache(maxsize=None)
def get_models_config() -> dict:
    return _load("models.yaml")


def reload_configs():
    """Call this to hot-reload configs without restart."""
    get_settings.cache_clear()
    get_apis.cache_clear()
    get_models_config.cache_clear()
