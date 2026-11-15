"""Dynamic choice sources (``core/choices.py``): the Ollama model list and failure handling."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from various_tools.core import choices
from various_tools.core.choices import dynamic_choices, ollama_model_names, register_source
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.settings import Settings


@pytest.fixture
def server() -> Iterator[tuple[str, dict[str, Any]]]:
    """A local stand-in for Ollama. ``state["body"]`` is sent for GET /api/tags."""
    state: dict[str, Any] = {"body": b"", "status": 200, "paths": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state["paths"].append(self.path)
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *args: object) -> None:
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    finally:
        httpd.shutdown()
        httpd.server_close()


def _ctx(tmp_path: Path, url: str | None, *, reachable: bool = True) -> RunContext:
    values: dict[str, Any] = {"onedrive_root": tmp_path}
    if url is not None:
        values["ollama_url"] = url
    return RunContext(
        settings=Settings.model_validate(values),
        deps=ExternalTools(env={}, ollama_probe=lambda _u: "0.1" if reachable else None),
    )


def _tags(*names: str) -> bytes:
    return json.dumps({"models": [{"name": n, "size": 1} for n in names]}).encode()


def test_ollama_models_come_from_api_tags(
    tmp_path: Path, server: tuple[str, dict[str, Any]]
) -> None:
    url, state = server
    state["body"] = _tags("qwen2.5vl:7b", "Llama3.2-vision:latest", "lfm3-vl:latest")
    found = dynamic_choices("ollama_models", _ctx(tmp_path, url))
    assert found == ["lfm3-vl:latest", "Llama3.2-vision:latest", "qwen2.5vl:7b"]
    assert state["paths"] == ["/api/tags"]


def test_the_saved_ollama_address_and_timeout_are_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake(url: str, timeout: float) -> list[str]:
        seen.update(url=url, timeout=timeout)
        return ["m:1"]

    monkeypatch.setattr(choices, "ollama_model_names", fake)
    ctx = RunContext(
        settings=Settings.model_validate(
            {
                "onedrive_root": tmp_path,
                "ollama_url": "http://box:11434",
                "ollama_tags_timeout_s": 3,
            }
        ),
        deps=ExternalTools(
            env={}, ollama_probe=lambda u: "0.1" if u == "http://box:11434" else None
        ),
    )
    assert dynamic_choices("ollama_models", ctx) == ["m:1"]
    assert seen == {"url": "http://box:11434", "timeout": 3}


def test_a_server_that_is_down_gives_an_empty_list(tmp_path: Path) -> None:
    assert dynamic_choices("ollama_models", _ctx(tmp_path, None, reachable=False)) == []


def test_a_closed_port_gives_an_empty_list(tmp_path: Path) -> None:
    # Nothing listens on port 9, and the probe says the server answered, so the request fails.
    assert dynamic_choices("ollama_models", _ctx(tmp_path, "http://127.0.0.1:9")) == []


@pytest.mark.parametrize(
    "body",
    [b"not json at all", b"[]", b'{"models": 5}', b'{"models": [5, {"nope": 1}, null]}', b""],
)
def test_bad_replies_give_an_empty_list(
    tmp_path: Path, server: tuple[str, dict[str, Any]], body: bytes
) -> None:
    url, state = server
    state["body"] = body
    assert dynamic_choices("ollama_models", _ctx(tmp_path, url)) == []


def test_an_error_status_gives_an_empty_list(
    tmp_path: Path, server: tuple[str, dict[str, Any]]
) -> None:
    url, state = server
    state["status"] = 500
    assert dynamic_choices("ollama_models", _ctx(tmp_path, url)) == []


def test_a_timeout_gives_an_empty_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(url: str, timeout: float) -> list[str]:
        raise TimeoutError("timed out")

    monkeypatch.setattr(choices, "ollama_model_names", slow)
    assert dynamic_choices("ollama_models", _ctx(tmp_path, None)) == []


def test_only_web_addresses_are_requested(tmp_path: Path) -> None:
    target = tmp_path / "tags.json"
    target.write_bytes(_tags("secret:latest"))
    with pytest.raises(ValueError):
        ollama_model_names(target.as_uri(), 1)
    assert dynamic_choices("ollama_models", _ctx(tmp_path, target.as_uri())) == []


def test_models_are_listed_once_and_without_blanks(
    tmp_path: Path, server: tuple[str, dict[str, Any]]
) -> None:
    url, state = server
    state["body"] = _tags("b:1", "a:1", "b:1", "  ")
    assert ollama_model_names(url, 5) == ["a:1", "b:1"]


def test_an_unknown_source_gives_an_empty_list(tmp_path: Path) -> None:
    assert dynamic_choices("no_such_source", _ctx(tmp_path, None)) == []
    assert dynamic_choices("", _ctx(tmp_path, None)) == []


def test_sources_can_be_registered_and_a_crashing_source_gives_an_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(choices, "_SOURCES", dict(choices._SOURCES))  # pyright: ignore[reportPrivateUsage]
    register_source("fruit", lambda _ctx: ["apple", "pear"])

    def broken(_ctx: RunContext) -> list[str]:
        raise RuntimeError("boom")

    register_source("broken", broken)
    ctx = _ctx(tmp_path, None)
    assert dynamic_choices("fruit", ctx) == ["apple", "pear"]
    assert dynamic_choices("broken", ctx) == []
