from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query

from .scraper.parser import ThreadsParser
from .scraper.threads_scraper import ThreadsScraper

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"

app = FastAPI(
    title="Threads Scraper API",
    description="HTTP API wrapper for scraping public Threads posts by username.",
    version="1.0.0",
)


def load_settings() -> Dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def expected_api_key() -> str:
    load_dotenv()
    return os.getenv("API_KEY", "")


def validate_api_key(
    query_api_key: Optional[str], header_api_key: Optional[str]
) -> None:
    configured_key = expected_api_key()
    provided_key = query_api_key or header_api_key
    if not configured_key or provided_key != configured_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


def parse_items(raw_items: List[Dict[str, Any]], username: str) -> List[Dict[str, Any]]:
    parser = ThreadsParser()
    parsed_items = [
        parser.parse_item(item, default_username=username) for item in raw_items
    ]
    return [item for item in parsed_items if item]


@app.get("/")
def scrape_user_threads(
    username: Optional[str] = Query(
        default=None, description="Threads username without @"
    ),
    apikey: Optional[str] = Query(
        default=None, description="API key. Prefer x-api-key header for production use."
    ),
    limit: Optional[int] = Query(
        default=None, ge=1, le=10, description="Maximum posts to return."
    ),
    offline: Optional[bool] = Query(
        default=None, description="Override settings.yaml use_offline."
    ),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
) -> Dict[str, Any]:
    validate_api_key(apikey, x_api_key)

    if not username or not username.strip():
        raise HTTPException(status_code=422, detail="username is required")

    normalized_username = username.strip().lstrip("@")
    settings = load_settings()
    if offline is not None:
        settings["use_offline"] = offline
    effective_limit = limit or int(settings.get("limit", 10))

    scraper = ThreadsScraper(
        settings=settings,
        config_dir=CONFIG_DIR,
        data_dir=DATA_DIR,
    )
    raw_items = scraper.fetch_user_threads(
        username=normalized_username, limit=effective_limit
    )
    items = parse_items(raw_items, username=normalized_username)

    return {
        "username": normalized_username,
        "count": len(items),
        "items": items,
    }
