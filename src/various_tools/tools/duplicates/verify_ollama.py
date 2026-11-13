"""Optional second opinion for borderline image matches, from a local Ollama vision model.

The model is asked whether two photos are the same. Only a clear "YES" confirms a match.
Any error counts as "not confirmed", so nothing is moved on a failed check (as in the legacy
script). The server address comes from ``core/deps.py``; this module never looks for Ollama itself.
"""

from __future__ import annotations

import base64
import io
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps

from various_tools.core.media.imaging import open_image
from various_tools.core.settings import (
    DEFAULT_OLLAMA_CHAT_TIMEOUT_S,
    DEFAULT_OLLAMA_TAGS_TIMEOUT_S,
)

# Defaults of the ``ollama_*`` options on ``DuplicatesOptions``. The verifier gets the option
# values, and the two timeouts come from the user's settings.
JPEG_SIZE = 512
JPEG_QUALITY = 85
PROMPT = "Image 1 and image 2 are photos. Are they the same photo? Answer only YES or NO."


@dataclass
class VerifyStats:
    checked: int = 0
    rejected: int = 0
    errors: int = 0


@dataclass
class OllamaVerifier:
    url: str
    model: str
    timeout: float = DEFAULT_OLLAMA_CHAT_TIMEOUT_S  # seconds per answer (``ollama_chat_timeout_s``)
    check_timeout: float = DEFAULT_OLLAMA_TAGS_TIMEOUT_S  # seconds to list models
    prompt: str = PROMPT
    image_px: int = JPEG_SIZE
    jpeg_quality: int = JPEG_QUALITY
    stats: VerifyStats = field(default_factory=VerifyStats)
    # Why the most recent ``confirms`` call failed, or None when it got an answer.
    last_error: str | None = None

    def check_model(self) -> str | None:
        """Why the model cannot be used, or None when it is installed."""
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=self.check_timeout) as resp:
                names = {str(m["name"]) for m in json.load(resp)["models"]}
        except Exception as exc:  # any failure here means the server is not usable
            return f"Ollama is not reachable at {self.url}: {exc}"
        if self.model not in names and f"{self.model}:latest" not in names:
            return f"Model {self.model} is not installed. Run: ollama pull {self.model}"
        return None

    def confirms(self, a: Path, b: Path) -> bool:
        """True only when the model answers YES for the pair."""
        self.stats.checked += 1
        self.last_error = None
        try:
            same = self._ask(a, b)
        except Exception as exc:  # an unreadable file or a dropped connection: not confirmed
            self.stats.errors += 1
            self.last_error = str(exc) or type(exc).__name__
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()  # an error response holds a temp file open
            return False
        if not same:
            self.stats.rejected += 1
        return same

    def _ask(self, a: Path, b: Path) -> bool:
        body = {
            "model": self.model,
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": 16},
            "messages": [
                {
                    "role": "user",
                    "content": self.prompt,
                    "images": [self._jpeg_b64(a), self._jpeg_b64(b)],
                }
            ],
        }
        request = urllib.request.Request(
            f"{self.url}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            answer = str(json.load(resp)["message"]["content"])
        answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.S).strip().upper()
        return answer.startswith("YES")

    def _jpeg_b64(self, path: Path) -> str:
        """A small, upright JPEG of the picture, base64 encoded for the model."""
        with open_image(path) as im:
            oriented: Image.Image = ImageOps.exif_transpose(im).convert("RGB")
            oriented.thumbnail((self.image_px, self.image_px))
            buf = io.BytesIO()
            oriented.save(buf, "JPEG", quality=self.jpeg_quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")
