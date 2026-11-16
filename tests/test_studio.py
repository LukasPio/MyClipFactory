import base64
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from clipfactory.api import create_app
from clipfactory.captions import subtitles
from clipfactory.config import Settings
from clipfactory.media import ffmpeg, probe, run
from clipfactory.pipeline import Worker
from clipfactory.publisher import oauth, queue
from clipfactory.schemas import Captions
from clipfactory.store import Store, uid
from clipfactory.transcription import transcribe

from .fixture import fixture_transcript, sample


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg exigido")
class StudioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.source = sample(Path(cls.directory.name) / "original.mp4")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings()
        self.settings.data = Path(directory.name)
        self.settings.origin = "http://testserver"
        self.settings.password = "test-secret"
        self.settings.key = Fernet.generate_key().decode()
        self.settings.public_origin = "https://media.example.test"
        self.store = Store(self.settings)
        self.client = TestClient(create_app(self.settings), headers={"Authorization": "Basic " +
                                 base64.b64encode(b"operator:test-secret").decode()})
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.__enter__()
        self.worker = Worker(self.settings, self.store, asr=fixture_transcript)

    def send(self):
        with self.source.open("rb") as file:
            response = self.client.post("/api/videos?name=gameplay.mp4", content=file,
                                        headers={"Content-Type": "video/mp4"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def process(self, source, **overrides):
        data = {"min_seconds": 9, "max_seconds": 18, "language": "en", "detect_webcam": False,
                "webcam": {"x": 0, "y": 0, "w": .23, "h": .31}} | overrides
        response = self.client.post(f"/api/videos/{source}/process", json=data)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["id"]

    def drain(self):
        for _ in range(12):
            job = self.store.claim_job()
            if not job:
                return
            self.worker.run_job(job, Event())
        self.fail("A fila não esvaziou.")

    def library(self):
        return self.client.get("/api/library").json()

    def account(self, platform):
        account = uid()
        with self.store.tx(True) as conn:
            conn.execute("INSERT INTO accounts VALUES(?,?,?,?,?)",
                         (account, platform, "external", "Creator",
                          oauth.encrypt({"access_token": "private-token", "expires_in": 3600}, self.settings)))
        return account

    def allow_destination(self, clip, account, privacy="private", due=None):
        return self.client.post(f"/api/clips/{clip['id']}/publish", json={
            "account_id": account, "revision": clip["revision"], "title": "Authorized title",
            "description": "Authorized description", "hashtags": ["Gameplay"], "privacy": privacy,
            "consent": True, "due": due,
        })

    def test_upload_rejects_corrupt_mp4_and_cross_site(self):
        for headers, expected in [
            ({"Content-Type": "video/mp4"}, 422),
            ({"Content-Type": "video/mp4", "Origin": "https://elsewhere.com"}, 403),
            ({"Content-Type": "text/plain"}, 415),
        ]:
            result = self.client.post("/api/videos?name=damaged.mp4", content=b"invalid", headers=headers)
            self.assertEqual(result.status_code, expected)
        self.assertEqual(self.library()["videos"], [])
        self.assertFalse(list((self.settings.data / "sources").glob("*")))

    def test_queue_cancel_and_retry(self):
        video = self.send()
        first = self.process(video["id"])
        duplicate = self.client.post(f"/api/videos/{video['id']}/process", json={})
        self.assertEqual(duplicate.status_code, 409)
        self.client.post(f"/api/jobs/{first}/cancel").raise_for_status()
        self.assertIsNone(self.store.claim_job())
        response = self.client.post(f"/api/jobs/{first}/retry")
        self.assertEqual(response.status_code, 202, response.text)
        job = self.store.claim_job()
        self.assertNotEqual(first, job["id"])
        with self.store.tx(True) as conn:
            conn.execute("UPDATE jobs SET lease=0 WHERE id=?", (job["id"],))
        recovered = self.store.claim_job()
        self.assertEqual(recovered["id"], job["id"])
        self.assertNotEqual(recovered["owner"], job["owner"])

    def test_multimodal_edit_render_and_publication_contracts(self):
        video = self.send()
        job = self.process(video["id"])
        self.drain()
        data = self.library()
        clips = data["clips"]
        self.assertGreaterEqual(len(clips), 2, data["jobs"])
        self.assertTrue(all(item["state"] == "DONE" for item in data["jobs"]), data["jobs"])
        self.assertEqual(self.client.get(f"/api/jobs/{job}/events").json()[-1]["progress"], 100)
        clip = next((item for item in clips if item["candidate"]["layout"] == "webcam"), clips[0])
        self.assertTrue(clip["candidate"]["webcam"])
        self.assertTrue(0 <= clip["candidate"]["score"] <= 100)
        source_id = clip["id"]
        published = self.account("youtube")
        instagram = self.account("instagram")
        self.assertNotIn("private-token", json.dumps(self.library()))
        self.assertEqual(self.allow_destination(clip, published).status_code, 409)
        route = f"/api/clips/{source_id}/video"
        response = self.client.get(route, headers={"Range": "bytes=0-127"})
        self.assertEqual(response.status_code, 206)
        path = self.settings.file("clips", f"{source_id}-1.mp4")
        media = probe(path, 90)
        self.assertEqual((media["width"], media["height"], media["codec"]), (1080, 1920, "h264"))
        self.assertEqual(media["audio"][0]["codec"], "aac")
        self.assertAlmostEqual(media["duration"], clip["fields"]["end"] - clip["fields"]["start"], delta=.17)
        self.assertIn("attachment", self.client.get(route + "?download=true").headers["Content-Disposition"])

        transcript = self.client.get(f"/api/clips/{source_id}/transcript").json()
        active_segment = next(s for s in transcript["segments"] if clip["fields"]["start"] <= s["start"] < clip["fields"]["end"])
        self.client.post(f"/api/clips/{source_id}/review", json={"decision": "APPROVED", "revision": 1}).raise_for_status()
        pending = self.allow_destination(clip, instagram, "", time.time() + 3000)
        self.assertEqual(pending.json()["state"], "SCHEDULED", pending.text)
        self.assertIsNone(queue.begin(self.store))
        corrected = clip["fields"] | {"title": "Revised cut", "corrections": [{"id": active_segment["id"], "text": "Revised caption."}]}
        result = self.client.put(f"/api/clips/{source_id}?revision=1", json=corrected)
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(self.client.get(route).status_code, 409)
        self.assertEqual(self.library()["publications"][0]["state"], "FAILED")
        self.drain()
        current = next(item for item in self.library()["clips"] if item["id"] == source_id)
        self.assertEqual((current["revision"], current["rendered_revision"]), (2, 2))
        self.assertEqual(self.allow_destination(current, published).status_code, 409)
        self.client.post(f"/api/clips/{source_id}/review", json={"decision": "APPROVED", "revision": 2}).raise_for_status()
        self.assertEqual(self.allow_destination(current, published, "public").status_code, 422)
        first = self.allow_destination(current, published)
        duplicate = self.allow_destination(current, published)
        self.assertEqual(first.json()["id"], duplicate.json()["id"])
        done = self.allow_destination(current, instagram, "")
        self.assertEqual(done.status_code, 202, done.text)

        requests = []

        def handler(request):
            path = request.url.path
            requests.append((request.method, path))
            if request.url.host == "www.googleapis.com":
                if path.startswith("/upload/"):
                    return httpx.Response(200, headers={"Location": "https://www.googleapis.com/session"})
                if path == "/session":
                    if request.headers["Content-Range"].startswith("bytes */"):
                        return httpx.Response(308)
                    return httpx.Response(201, json={"id": "video42"})
                if path == "/youtube/v3/videos":
                    return httpx.Response(200, json={"items": [{"processingDetails": {"processingStatus": "succeeded"},
                                                               "status": {"uploadStatus": "processed", "privacyStatus": "private"}}]})
            if path.endswith("/external/media"):
                uri = httpx.QueryParams(request.content.decode())["video_url"]
                signed_path = uri.removeprefix(self.settings.public_origin)
                self.client.get(signed_path, headers={"Range": "bytes=0-10"}).raise_for_status()
                self.assertEqual(self.client.get(signed_path + "wrong").status_code, 403)
                return httpx.Response(200, json={"id": "container"})
            if path.endswith("/container"):
                return httpx.Response(200, json={"status_code": "FINISHED"})
            if path.endswith("/external/media_publish"):
                return httpx.Response(200, json={"id": "media"})
            if path.endswith("/media"):
                return httpx.Response(200, json={"id": "media", "permalink": "https://www.instagram.com/reel/media/"})
            self.fail(f"Requisição não prevista: {request.url}")

        with httpx.Client(transport=httpx.MockTransport(handler)) as fake:
            for _ in range(5):
                self.assertTrue(queue.work(self.store, self.settings, fake))
                with self.store.tx(True) as conn:
                    conn.execute("UPDATE publications SET retry_at=0 WHERE state='PROCESSING'")
        history = {item["id"]: item for item in self.library()["publications"]}
        self.assertEqual(history[first.json()["id"]]["url"], "https://www.youtube.com/shorts/video42")
        self.assertEqual(history[done.json()["id"]]["state"], "PUBLISHED")
        self.assertEqual(sum(path.endswith("/media_publish") for _, path in requests), 1)

    @unittest.skipUnless(os.getenv("MCF_TEST_WHISPER") == "1", "Teste opt-in do modelo Whisper real")
    def test_real_whisper_english_timestamps(self):
        self.settings.model = os.getenv("MCF_TEST_MODEL", "tiny")
        self.worker.asr = transcribe
        video = self.send()
        # Run only the analysis job. Rendering, review and delivery are covered independently.
        self.process(video["id"])
        self.worker.run_job(self.store.claim_job(), Event())
        with self.store.tx() as conn:
            row = conn.execute("SELECT transcript FROM analysis_cache").fetchone()
        self.assertIsNotNone(row, self.library()["jobs"])
        segments = json.loads(row["transcript"])["segments"]
        self.assertGreaterEqual(len(segments), 2)
        self.assertTrue(any(word["confidence"] > .5 and 0 <= word["start"] < word["end"] <= 42
                            for segment in segments for word in segment["words"]))


class CaptionTest(unittest.TestCase):
    def test_relative_timestamps_and_inaudible_label(self):
        transcript = {"segments": [
            {"id": 1, "text": "Confabulado", "start": 9.0, "end": 11.1, "words": [], "unclear": True},
            {"id": 2, "text": "Escrito errado", "start": 12.5, "end": 13.5, "words": [], "unclear": False},
        ]}
        edit = {"start": 10.0, "end": 15.0, "captions": Captions().model_dump(),
                "corrections": [{"id": 2, "text": "Texto corrigido"}]}
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "subtitle.ass"
            subtitles(file, transcript, edit, None)
            result = file.read_text()
        self.assertIn("0:00:00.00,0:00:01.10", result)
        self.assertIn("[inaudível]", result)
        self.assertIn("0:00:02.50,0:00:03.50", result)
        self.assertIn("corrigido", result)
        self.assertNotIn("Confabulado", result)
