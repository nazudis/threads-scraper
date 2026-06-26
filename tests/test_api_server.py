import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api_server import app
from src.scraper.threads_scraper import InvalidUsernameError


class ApiServerTest(unittest.TestCase):
    def test_rejects_missing_api_key(self):
        client = TestClient(app)

        response = client.get("/", params={"username": "nazu_dis"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Invalid API key"})

    def test_rejects_missing_username(self):
        with patch.dict(os.environ, {"API_KEY": "123123"}, clear=False):
            client = TestClient(app)

            response = client.get("/", params={"apikey": "123123"})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "username is required"})

    def test_scrapes_user_threads_with_query_api_key(self):
        sample_raw_items = [
            {
                "id": "post-1",
                "username": "nazu_dis",
                "text": "hello threads",
                "like_count": 3,
                "reply_count": 1,
                "repost_count": 0,
                "created_at": "2026-01-01T00:00:00Z",
                "url": "https://www.threads.net/@nazu_dis/post/post-1",
            }
        ]

        class FakeScraper:
            def __init__(self, settings, config_dir, data_dir):
                self.settings = settings
                self.config_dir = config_dir
                self.data_dir = data_dir

            def fetch_user_threads(self, username, limit):
                self.username = username
                self.limit = limit
                return sample_raw_items

        with patch.dict(os.environ, {"API_KEY": "123123"}, clear=False):
            with patch("src.api_server.ThreadsScraper", FakeScraper):
                client = TestClient(app)
                response = client.get(
                    "/",
                    params={"username": "nazu_dis", "apikey": "123123", "limit": 5},
                )

        self.assertEqual(response.status_code, 200)
        expected_items = [
            dict(sample_raw_items[0], created_at="2026-01-01T07:00:00+07:00")
        ]
        self.assertEqual(
            response.json(),
            {"username": "nazu_dis", "count": 1, "items": expected_items},
        )

    def test_returns_invalid_username_error_when_scraper_cannot_resolve_user(self):
        class FakeScraper:
            def __init__(self, settings, config_dir, data_dir):
                pass

            def fetch_user_threads(self, username, limit):
                raise InvalidUsernameError("invalid username")

        with patch.dict(os.environ, {"API_KEY": "123123"}, clear=False):
            with patch("src.api_server.ThreadsScraper", FakeScraper):
                client = TestClient(app)
                response = client.get(
                    "/",
                    params={"username": "missing_user", "apikey": "123123"},
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "invalid username"})

    def test_accepts_header_api_key(self):
        class FakeScraper:
            def __init__(self, settings, config_dir, data_dir):
                pass

            def fetch_user_threads(self, username, limit):
                return []

        with patch.dict(os.environ, {"API_KEY": "123123"}, clear=False):
            with patch("src.api_server.ThreadsScraper", FakeScraper):
                client = TestClient(app)
                response = client.get(
                    "/",
                    params={"username": "nazu_dis"},
                    headers={"x-api-key": "123123"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"username": "nazu_dis", "count": 0, "items": []}
        )


if __name__ == "__main__":
    unittest.main()
