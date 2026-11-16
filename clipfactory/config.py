import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class Settings:
    def __init__(self):
        self.data = Path(os.getenv("MCF_DATA_DIR", "data")).resolve()
        self.origin = os.getenv("MCF_ORIGIN", "http://127.0.0.1:8000").rstrip("/")
        self.password = os.getenv("MCF_PASSWORD", "")
        if not self.password and self.origin not in {"http://127.0.0.1:8000", "http://localhost:8000"}:
            raise RuntimeError("Configure MCF_PASSWORD para servir fora da origem local padrão.")
        self.max_bytes = int(os.getenv("MCF_MAX_UPLOAD_BYTES", str(30 * 1024**3)))
        self.max_hours = float(os.getenv("MCF_MAX_HOURS", "12"))
        self.model = os.getenv("MCF_WHISPER_MODEL", "small")
        self.device = os.getenv("MCF_WHISPER_DEVICE", "cpu")
        self.compute = os.getenv("MCF_WHISPER_COMPUTE", "int8")
        self.vision_model = os.getenv("MCF_VISION_MODEL", "")
        self.vision_origin = os.getenv("MCF_VISION_ORIGIN", "http://127.0.0.1:11434").rstrip("/")
        self.key = os.getenv("MCF_TOKEN_KEY", "")
        self.public_origin = os.getenv("MCF_MEDIA_HTTPS_ORIGIN", "").rstrip("/")
        self.instagram_version = os.getenv("MCF_INSTAGRAM_VERSION", "v25.0")
        self.tiktok_audited = os.getenv("MCF_TIKTOK_AUDITED") == "1"
        self.tiktok_pull = os.getenv("MCF_TIKTOK_PULL_FROM_URL") == "1"
        self.tiktok_enabled = os.getenv("MCF_TIKTOK_DIRECT_POST") == "1"
        self.youtube_audited = os.getenv("MCF_YOUTUBE_AUDITED") == "1"

    def file(self, category, name):
        directory = self.data / category
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return directory / name
