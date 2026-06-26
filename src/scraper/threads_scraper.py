"""
ThreadsScraper - Fetch public Threads posts via the unofficial web API.

Uses the same GraphQL endpoints that the Threads web app calls internally.
No login required for public profiles and search.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .utils.logger import get_logger
from .utils.error_handler import retry

logger = get_logger(__name__)

# Headers for GraphQL API requests
API_HEADERS = {
    "authority": "www.threads.net",
    "accept": "*/*",
    "accept-language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "cache-control": "no-cache",
    "origin": "https://www.threads.net",
    "pragma": "no-cache",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "x-fb-lsd": "default",
    "x-ig-app-id": "238260118697367",
}

# Headers for browser page requests (triggers server-side rendering)
PAGE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

GRAPHQL_URL = "https://www.threads.net/api/graphql"


class InvalidUsernameError(Exception):
    """Raised when a Threads username cannot be resolved to a user ID."""


class ThreadsScraper:
    """Fetch public Threads posts for given usernames or search keywords."""

    def __init__(
        self,
        settings: Optional[Dict[str, Any]] = None,
        config_dir: Optional[Path] = None,
        data_dir: Optional[Path] = None,
    ):
        settings = settings or {}
        self.timeout = settings.get("timeout", 15)
        self.use_offline = settings.get("use_offline", False)
        self.data_dir = Path(data_dir) if data_dir else Path("data")

        # Separate sessions for API and page requests
        self.api_session = requests.Session()
        self.api_session.headers.update(API_HEADERS)

        self.page_session = requests.Session()
        self.page_session.headers.update(PAGE_HEADERS)

        self._lsd_token: Optional[str] = None

    def _get_lsd_token(self) -> str:
        """Fetch an LSD token from the Threads homepage."""
        if self._lsd_token:
            return self._lsd_token
        try:
            resp = self.page_session.get(
                "https://www.threads.net/@instagram",
                timeout=self.timeout,
            )
            match = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', resp.text)
            if match:
                self._lsd_token = match.group(1)
                logger.info(f"Obtained LSD token: {self._lsd_token[:8]}...")
                return self._lsd_token
        except Exception as e:
            logger.warning(f"Failed to get LSD token: {e}")
        self._lsd_token = "default"
        return self._lsd_token

    @retry(exceptions=(requests.RequestException, json.JSONDecodeError), tries=3, delay=1.0)
    def _graphql_request(self, doc_id: str, variables: Dict[str, Any]) -> Any:
        """Make a GraphQL request to the Threads API."""
        lsd = self._get_lsd_token()
        data = {
            "lsd": lsd,
            "variables": json.dumps(variables),
            "doc_id": doc_id,
        }
        headers = {
            **API_HEADERS,
            "content-type": "application/x-www-form-urlencoded",
            "x-fb-lsd": lsd,
        }
        resp = self.api_session.post(
            GRAPHQL_URL,
            data=data,
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _fetch_profile_page(self, username: str) -> str:
        """Fetch a profile page using browser-like headers for server-side rendering."""
        resp = self.page_session.get(
            f"https://www.threads.net/@{username}",
            timeout=self.timeout,
        )
        return resp.text

    def _get_user_id(self, username: str) -> Optional[str]:
        """Get the user ID from a username by scraping the profile page."""
        try:
            html = self._fetch_profile_page(username)

            # Pattern 1: "pk":"63458556663" in embedded JSON
            match = re.search(r'"pk":"(\d+)"', html)
            if match:
                logger.info(f"Found user ID via pk for @{username}: {match.group(1)}")
                return match.group(1)

            # Pattern 2: "userID":"63458556663" in relay data
            match = re.search(r'"userID":"(\d+)"', html)
            if match:
                logger.info(f"Found user ID via userID for @{username}: {match.group(1)}")
                return match.group(1)

            # Pattern 3: "user_id":"..." in cookie data
            match = re.search(r'"user_id":"(\d+)"', html)
            if match:
                return match.group(1)

            logger.warning(f"No user ID found in HTML for @{username} (page size: {len(html)} bytes)")
        except Exception as e:
            logger.warning(f"Failed to get user ID for @{username}: {e}")
        return None

    def _extract_threads_from_html(self, html: str, username: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Extract thread posts from embedded script data in profile HTML."""
        threads = []

        # Find all "thread_items" occurrences and extract arrays using balanced brackets
        for match in re.finditer(r'"thread_items"\s*:', html):
            pos = match.end()
            arr_start = html.find("[", pos)
            if arr_start < 0 or arr_start > pos + 10:
                continue

            # Balanced bracket matching to find the full array
            depth = 0
            end = -1
            for j in range(arr_start, min(arr_start + 200000, len(html))):
                if html[j] == "[":
                    depth += 1
                elif html[j] == "]":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            if end < 0:
                continue

            try:
                items = json.loads(html[arr_start:end + 1])

                for item in items:
                    post = item.get("post", {})
                    if not post:
                        continue
                    user = post.get("user", {})
                    caption = post.get("caption", {})
                    text = caption.get("text", "") if isinstance(caption, dict) else str(caption or "")

                    threads.append({
                        "id": str(post.get("pk") or post.get("id") or ""),
                        "username": user.get("username") or username,
                        "text": text,
                        "like_count": post.get("like_count", 0),
                        "reply_count": (
                            post.get("text_post_app_info", {}).get("direct_reply_count", 0)
                            if isinstance(post.get("text_post_app_info"), dict)
                            else 0
                        ),
                        "repost_count": post.get("repost_count", 0),
                        "created_at": post.get("taken_at"),
                        "url": f"https://www.threads.net/@{user.get('username', username)}/post/{post.get('code', '')}",
                    })
            except (json.JSONDecodeError, TypeError, KeyError):
                continue

        # Deduplicate by id and by text content
        seen_ids = set()
        seen_texts = set()
        unique = []
        for t in threads:
            tid = t["id"]
            text_key = t["text"][:100] if t["text"] else ""
            if tid and tid in seen_ids:
                continue
            if text_key and text_key in seen_texts:
                continue
            if tid:
                seen_ids.add(tid)
            if text_key:
                seen_texts.add(text_key)
            unique.append(t)

        return unique[:limit]

    def fetch_user_threads(
        self, username: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Fetch threads for a given username."""
        if self.use_offline:
            return self._load_offline_data(username)

        # Strategy 1: Fetch profile page and extract embedded data directly
        # This works for most accounts with server-side rendered pages
        try:
            html = self._fetch_profile_page(username)
            page_size = len(html)
            logger.info(f"Profile page for @{username}: {page_size} bytes")

            # If page is large enough (>300KB), it's likely server-rendered with data
            if page_size > 300000:
                threads = self._extract_threads_from_html(html, username, limit)
                if threads:
                    logger.info(f"Extracted {len(threads)} threads for @{username} from embedded HTML data")
                    return threads

            # Try to get user_id from the HTML we already fetched
            user_id = None
            match = re.search(r'"pk":"(\d+)"', html)
            if match:
                user_id = match.group(1)
            else:
                match = re.search(r'"userID":"(\d+)"', html)
                if match:
                    user_id = match.group(1)

            if not user_id:
                logger.warning(f"Could not resolve user ID for @{username} (page: {page_size} bytes)")
                raise InvalidUsernameError("invalid username")

        except InvalidUsernameError:
            raise
        except Exception as e:
            logger.warning(f"Failed to fetch profile page for @{username}: {e}")
            return []

        # Strategy 2: Use GraphQL API with user_id
        try:
            variables = {
                "userID": user_id,
                "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": False,
                "__relay_internal__pv__BarcelonaIsThreadContextHeaderEnabledrelayprovider": False,
            }
            result = self._graphql_request(
                doc_id="6232751443445612",  # BarcelonaProfileThreadsTabQuery
                variables=variables,
            )

            threads = []
            raw_threads = (
                result.get("data", {})
                .get("mediaData", {})
                .get("threads", [])
            )
            if isinstance(raw_threads, dict):
                raw_threads = raw_threads.get("edges", [])
                raw_threads = [e.get("node", e) for e in raw_threads]

            for thread_node in raw_threads[:limit]:
                thread_items = thread_node.get("thread_items", [])
                for item in thread_items:
                    post = item.get("post", {})
                    user = post.get("user", {})
                    caption = post.get("caption", {})
                    text = caption.get("text", "") if isinstance(caption, dict) else str(caption or "")

                    threads.append({
                        "id": str(post.get("pk") or post.get("id") or ""),
                        "username": user.get("username") or username,
                        "text": text,
                        "like_count": post.get("like_count", 0),
                        "reply_count": (
                            post.get("text_post_app_info", {}).get("direct_reply_count", 0)
                            if isinstance(post.get("text_post_app_info"), dict)
                            else 0
                        ),
                        "repost_count": post.get("repost_count", 0),
                        "created_at": post.get("taken_at"),
                        "url": f"https://www.threads.net/@{username}/post/{post.get('code', '')}",
                    })

            logger.info(f"Fetched {len(threads)} threads for @{username} via GraphQL")
            return threads

        except Exception as e:
            logger.warning(f"GraphQL fetch failed for @{username}: {e}")
            return []

    def search_threads(self, keyword: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Search Threads for a keyword using the search API."""
        try:
            variables = {
                "query": keyword,
                "search_surface": "default",
                "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": False,
            }
            result = self._graphql_request(
                doc_id="6723348034398498",
                variables=variables,
            )

            threads = []
            search_results = (
                result.get("data", {})
                .get("searchResults", {})
                .get("edges", [])
            )

            for edge in search_results[:limit]:
                node = edge.get("node", {})
                thread_items = node.get("thread_items", [])

                for item in thread_items:
                    post = item.get("post", {})
                    user = post.get("user", {})
                    caption = post.get("caption", {})
                    text = caption.get("text", "") if isinstance(caption, dict) else str(caption or "")

                    if text:
                        threads.append({
                            "id": str(post.get("pk") or post.get("id") or ""),
                            "username": user.get("username", ""),
                            "text": text,
                            "like_count": post.get("like_count", 0),
                            "reply_count": (
                                post.get("text_post_app_info", {}).get("direct_reply_count", 0)
                                if isinstance(post.get("text_post_app_info"), dict)
                                else 0
                            ),
                            "repost_count": 0,
                            "created_at": post.get("taken_at"),
                            "url": f"https://www.threads.net/@{user.get('username', '')}/post/{post.get('code', '')}",
                        })

            if threads:
                logger.info(f"Found {len(threads)} results for '{keyword}' via search API")
                return threads

        except Exception as e:
            logger.warning(f"Search API failed for '{keyword}': {e}")

        # Fallback: scrape the search page
        return self._scrape_search_page(keyword, limit)

    def _scrape_search_page(self, keyword: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Scrape search results from the Threads search page."""
        try:
            resp = self.page_session.get(
                f"https://www.threads.net/search",
                params={"q": keyword, "serp_type": "default"},
                timeout=self.timeout,
            )

            threads = self._extract_threads_from_html(resp.text, "", limit)
            logger.info(f"Scraped {len(threads)} results for '{keyword}' from search page")
            return threads

        except Exception as e:
            logger.error(f"Search page scrape failed for '{keyword}': {e}")
            return []

    def _load_offline_data(self, username: str) -> List[Dict[str, Any]]:
        """Load offline sample data for development/testing."""
        raw_dir = self.data_dir / "raw"
        json_file = raw_dir / f"{username}.json"

        if json_file.exists():
            with open(json_file, "r", encoding="utf-8") as f:
                return json.load(f)

        logger.warning(f"No offline data for @{username}")
        return []
