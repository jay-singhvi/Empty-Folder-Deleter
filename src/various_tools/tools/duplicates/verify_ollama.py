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
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps

from various_tools.core.media.imaging import open_image

CHECK_TIMEOUT_S = 10
CHAT_TIMEOUT_S = 300  # a vision model can take a while per pair on modest hardware
JPEG_SIZE = 512
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
    timeout: float = CHAT_TIMEOUT_S
    stats: VerifyStats = field(default_factory=VerifyStats)

    def check_model(self) -> str | None:
        """Why the model cannot be used, or None when it is installed."""
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=CHECK_TIMEOUT_S) as resp:
                names = {str(m["name"]) for m in json.load(resp)["models"]}
        except Exception as exc:  # any failure here means the server is not usable
            return f"Ollama is not reachable at {self.url}: {exc}"
        if self.model not in names and f"{self.model}:latest" not in names:
            return f"Model {self.model} is not installed. Run: ollama pull {self.model}"
        return None

    def confirms(self, a: Path, b: Path) -> bool:
        """True only when the model answers YES for the pair."""
        self.stats.checked += 1
        try:
            same = self._ask(a, b)
        except Exception:  # an unreadable file or a dropped connection: not confirmed
            self.stats.errors += 1
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
                {"role": "user", "content": PROMPT, "images": [_jpeg_b64(a), _jpeg_b64(b)]}
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


def _jpeg_b64(path: Path) -> str:
    """A small, upright JPEG of the picture, base64 encoded for the model."""
    with open_image(path) as im:
        oriented: Image.Image = ImageOps.exif_transpose(im).convert("RGB")
        oriented.thumbnail((JPEG_SIZE, JPEG_SIZE))
        buf = io.BytesIO()
        oriented.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")
