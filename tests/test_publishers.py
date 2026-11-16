import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from clipfactory.config import Settings
from clipfactory.publisher import oauth, tiktok, youtube
from clipfactory.publisher.base import PublishError
from clipfactory.store import Store


class PublisherTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "test.mp4"
        self.path.write_bytes(b"x" * 12345)
        self.settings = Settings()
        self.settings.tiktok_enabled = True
        self.fields = {
            "title": "Clip", "description": "Description", "caption": "Caption", "hashtags": ["gameplay"],
            "privacy": "SELF_ONLY", "allow_comment": False, "allow_duet": False, "allow_stitch": False,
            "commercial": False, "own_brand": False, "branded_content": False, "duration": 19,
        }

    def test_tiktok_creator_upload_and_moderation_failure(self):
        state = {}
        requests = []

        def result(value):
            return httpx.Response(200, json={"data": value, "error": {"code": "ok"}})

        def handler(req):
            path = req.url.path
            requests.append(path)
            if path.endswith("creator_info/query/"):
                return result({"creator_username": "creator", "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
                               "max_video_post_duration_sec": 60})
            if path.endswith("video/init/"):
                body = json.loads(req.content)
                self.assertEqual(body["post_info"]["privacy_level"], "SELF_ONLY")
                self.assertEqual(body["source_info"]["total_chunk_count"], 1)
                return result({"publish_id": "v_pub_file~v2.01234",
                               "upload_url": "https://open-upload.tiktokapis.com/upload/video"})
            if path == "/upload/video":
                self.assertNotIn("Authorization", req.headers)
                self.assertEqual(req.headers["Content-Range"], "bytes 0-12344/12345")
                return httpx.Response(201)
            self.assertEqual(json.loads(req.content)["publish_id"], "v_pub_file~v2.01234")
            return result({"status": "FAILED", "fail_reason": "moderation_failed"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            args = (client, "secret", {}, self.path, self.fields, state,
                    lambda **updates: state.update(updates), self.settings, None)
            creator = tiktok.profile(client, "secret")
            with self.assertRaisesRegex(PublishError, "auditoria"):
                tiktok.validate(self.fields | {"privacy": "PUBLIC_TO_EVERYONE"}, creator, 19, self.settings)
            self.assertIsNone(tiktok.publish(*args).external_id)
            with self.assertRaisesRegex(PublishError, "moderation_failed"):
                tiktok.publish(*args)
        self.assertEqual(sum(path.endswith("video/init/") for path in requests), 1)

    def test_tiktok_pull_does_not_retry_ambiguous_initialization(self):
        self.settings.tiktok_pull = True
        checkpoint = {}
        calls = []

        def handler(req):
            if req.url.path.endswith("creator_info/query/"):
                return httpx.Response(200, json={"error": {"code": "ok"}, "data": {
                    "creator_username": "creator", "privacy_level_options": ["SELF_ONLY"],
                    "max_video_post_duration_sec": 60}})
            calls.append(json.loads(req.content)["source_info"])
            raise httpx.ReadTimeout("Response lost", request=req)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            args = (client, "secret", {}, self.path, self.fields, checkpoint,
                    lambda **updates: checkpoint.update(updates), self.settings,
                    lambda: "https://media.example.test/signed")
            with self.assertRaisesRegex(PublishError, "comunicação"):
                tiktok.publish(*args)
            with self.assertRaisesRegex(PublishError, "Resultado incerto"):
                tiktok.publish(*args)
        self.assertEqual(calls, [{"source": "PULL_FROM_URL", "video_url": "https://media.example.test/signed"}])

    def test_youtube_429_resumes_existing_session(self):
        state = {"session": "https://www.googleapis.com/session"}
        content_ranges = []

        def handler(req):
            content_ranges.append(req.headers["Content-Range"])
            if len(content_ranges) == 1:
                return httpx.Response(429, headers={"Retry-After": "42"})
            if len(content_ranges) == 2:
                return httpx.Response(308, headers={"Range": "bytes=0-1023"})
            self.assertEqual(len(req.content), 12345 - 1024)
            return httpx.Response(201, json={"id": "abc123"})

        settings = self.settings
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            args = (client, "secret", {}, self.path, self.fields | {"privacy": "private"}, state,
                    lambda **updates: state.update(updates), settings, None)
            with self.assertRaises(PublishError) as caught:
                youtube.publish(*args)
            self.assertTrue(caught.exception.retry)
            self.assertEqual(caught.exception.delay, 42)
            self.assertIsNone(youtube.publish(*args).external_id)

        self.assertEqual(content_ranges, ["bytes */12345", "bytes */12345", "bytes 1024-12344/12345"])
        self.assertEqual(state["video"], "abc123")

    def test_oauth_state_is_single_use_and_wrong_state_is_blocked(self):
        from cryptography.fernet import Fernet
        from urllib.parse import parse_qs, urlsplit

        settings = self.settings
        settings.data = self.path.parent
        settings.key = Fernet.generate_key().decode()
        store = Store(settings)
        store.init()

        def handler(req):
            if req.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "secret", "expires_in": 3600})
            return httpx.Response(200, json={"items": [{"id": "yt_channel", "snippet": {"title": "Channel"}}]})

        with patch.dict("os.environ", {"MCF_YOUTUBE_CLIENT_ID": "test", "MCF_YOUTUBE_CLIENT_SECRET": "secret"}):
            link = oauth.start("youtube", settings, store)
            state = parse_qs(urlsplit(link).query)["state"][0]
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(PublishError, "OAuth inválido"):
                    oauth.connect("youtube", "wrong", "code", client, settings, store)
                oauth.connect("youtube", state, "code", client, settings, store)
                with self.assertRaisesRegex(PublishError, "OAuth inválido"):
                    oauth.connect("youtube", state, "code", client, settings, store)
        with store.tx() as conn:
            account = conn.execute("SELECT * FROM accounts").fetchone()
        self.assertEqual(account["external_id"], "yt_channel")
        self.assertNotIn("secret", account["token"])
