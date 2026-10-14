import os
import sys

import yaml

try:  # .env is optional; values can also come from the real environment
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

REQUIRED_KEYS = ['keepa_api_key', 'discord_webhook_url', 'scraper', 'keepa', 'roi', 'sites']

# Secrets live in the environment (.env), never in config.yaml.
# Each entry maps a path inside the config to the env var that fills it.
ENV_OVERRIDES = {
    ('keepa_api_key',): 'KEEPA_API_KEY',
    ('discord_webhook_url',): 'DISCORD_WEBHOOK_URL',
    ('scraper', 'proxy', 'server'): 'PROXY_SERVER',
    ('scraper', 'proxy', 'username'): 'PROXY_USERNAME',
    ('scraper', 'proxy', 'password'): 'PROXY_PASSWORD',
    ('arbisource', 'webhook_url'): 'ARBISOURCE_WEBHOOK_URL',
    ('currys', 'webhook_url'): 'CURRYS_WEBHOOK_URL',
    ('hukd', 'webhook_url'): 'HUKD_WEBHOOK_URL',
    ('argos', 'webhook_url'): 'ARGOS_WEBHOOK_URL',
    ('tkmaxx', 'webhook_url'): 'TKMAXX_WEBHOOK_URL',
}


def apply_env(config: dict) -> dict:
    """Fill secret values in the config from environment variables."""
    for path, var in ENV_OVERRIDES.items():
        value = os.environ.get(var)
        if not value:
            continue
        node = config
        for key in path[:-1]:
            if not isinstance(node.get(key), dict):
                node[key] = {}
            node = node[key]
        node[path[-1]] = value
    return config


def read_yaml(path='config.yaml') -> dict:
    """Read config.yaml (if present) with env secrets applied. Never exits."""
    config = {}
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
    return apply_env(config)


def load_config(path='config.yaml'):
    config = read_yaml(path)

    for key in REQUIRED_KEYS:
        if key not in config:
            print(f"[config] Missing required key: '{key}' in {path}")
            sys.exit(1)

    if not config.get('sites'):
        print("[config] No sites defined in config.yaml")
        sys.exit(1)

    return config
