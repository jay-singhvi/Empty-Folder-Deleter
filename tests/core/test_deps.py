"""External program lookup (§3.5): override → env → PATH → known locations → disabled.

Fake programs are ``.bat`` files, which Windows runs directly, so the real version probe runs
against them. Nothing real is installed or run.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from various_tools.core import deps
from various_tools.core.deps import (
    DEFAULT_OLLAMA_URL,
    ExternalTools,
    ToolSource,
    install_hint,
)

FakeExe = Callable[..., Path]


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A private environment: no PATH hits, and the known-location roots point into tmp_path."""
    env = {
        "PATH": str(tmp_path / "empty-path"),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
        "ProgramFiles": str(tmp_path / "programfiles"),
        "ProgramFiles(x86)": str(tmp_path / "programfiles86"),
        "USERPROFILE": str(tmp_path / "profile"),
    }
    # Known-location templates are expanded from os.environ, so the variables must be real.
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(deps.shutil, "which", lambda _name: None)
    return env


def test_override_wins_when_it_works(
    tmp_path: Path, fake_exe: FakeExe, isolated_env: dict[str, str]
) -> None:
    tool = fake_exe(tmp_path / "custom", "exiftool", output="13.10")
    tools = ExternalTools({"exiftool": str(tool)}, env=isolated_env)
    location = tools.resolve("exiftool")
    assert location.valid
    assert location.source is ToolSource.OVERRIDE
    assert location.path == tool
    assert location.version == "13.10"


def test_invalid_override_falls_back_and_says_so(
    tmp_path: Path,
    fake_exe: FakeExe,
    isolated_env: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    good = fake_exe(tmp_path / "env", "exiftool", output="13.11")
    isolated_env["VARIOUS_TOOLS_EXIFTOOL"] = str(good)
    tools = ExternalTools(
        {"exiftool": str(tmp_path / "moved-away" / "exiftool.bat")}, env=isolated_env
    )
    location = tools.resolve("exiftool")
    assert location.source is ToolSource.ENVIRONMENT
    assert location.path == good
    assert any("no longer works" in p for p in location.problems)


def test_environment_variable_is_used_before_path(
    tmp_path: Path, fake_exe: FakeExe, isolated_env: dict[str, str]
) -> None:
    env_tool = fake_exe(tmp_path / "env", "ffmpeg", output="ffmpeg version 7.1")
    isolated_env["VARIOUS_TOOLS_FFMPEG"] = str(env_tool)
    tools = ExternalTools(env=isolated_env)
    location = tools.resolve("ffmpeg")
    assert location.source is ToolSource.ENVIRONMENT
    assert location.version == "ffmpeg version 7.1"


def test_path_lookup_is_used_when_no_override_or_env(
    tmp_path: Path, fake_exe: FakeExe, isolated_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    on_path = fake_exe(tmp_path / "bin", "ffmpeg", output="ffmpeg version 7.0")
    monkeypatch.setattr(
        deps.shutil, "which", lambda name: str(on_path) if name == "ffmpeg" else None
    )
    location = ExternalTools(env=isolated_env).resolve("ffmpeg")
    assert location.source is ToolSource.PATH
    assert location.path == on_path


def test_known_location_is_searched_last(
    tmp_path: Path, fake_exe: FakeExe, isolated_env: dict[str, str]
) -> None:
    # A WinGet-style package folder with a wildcard, matched through the spec's template.
    package_bin = (
        tmp_path
        / "localappdata"
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "Gyan.FFmpeg_test"
        / "ffmpeg-7.1-full"
        / "bin"
    )
    exe = fake_exe(package_bin, "ffmpeg", output="ffmpeg version 7.1 full")
    location = ExternalTools(env=isolated_env).resolve("ffmpeg")
    assert location.source is ToolSource.KNOWN_LOCATION
    assert location.path == exe


def test_ffprobe_is_found_next_to_ffmpeg(
    tmp_path: Path, fake_exe: FakeExe, isolated_env: dict[str, str]
) -> None:
    folder = tmp_path / "tools" / "ff"
    ffmpeg_exe = fake_exe(folder, "ffmpeg", output="ffmpeg version 7.1")
    ffprobe_exe = fake_exe(folder, "ffprobe", output="ffprobe version 7.1")
    isolated_env["VARIOUS_TOOLS_FFMPEG"] = str(ffmpeg_exe)
    tools = ExternalTools(env=isolated_env)
    assert tools.resolve("ffmpeg").valid
    probe = tools.resolve("ffprobe")
    assert probe.path == ffprobe_exe
    assert probe.source is ToolSource.KNOWN_LOCATION


def test_missing_tool_is_disabled_with_an_install_hint(isolated_env: dict[str, str]) -> None:
    location = ExternalTools(env=isolated_env).resolve("exiftool")
    assert not location.valid
    assert location.path is None
    assert location.source is ToolSource.NONE
    assert location.install_hint == "winget install OliverBetz.ExifTool"
    assert ExternalTools(env=isolated_env).path("exiftool") is None


def test_results_are_cached_until_redetect(
    tmp_path: Path, fake_exe: FakeExe, isolated_env: dict[str, str]
) -> None:
    calls: list[str] = []

    def counting_probe(path: Path, args: tuple[str, ...]) -> str | None:
        calls.append(path.name)
        return "1.0"

    tool = fake_exe(tmp_path / "custom", "exiftool")
    tools = ExternalTools({"exiftool": str(tool)}, env=isolated_env, version_probe=counting_probe)
    tools.resolve("exiftool")
    tools.resolve("exiftool")
    assert len(calls) == 1
    tools.redetect()
    tools.resolve("exiftool")
    assert len(calls) == 2


def test_version_probe_failure_means_not_valid(
    tmp_path: Path, isolated_env: dict[str, str]
) -> None:
    # A file that exists but does not answer the version command is not accepted.
    fake = tmp_path / "custom" / "exiftool.bat"
    fake.parent.mkdir(parents=True)
    fake.write_text("@echo off\r\nexit /b 3\r\n", encoding="ascii")
    tools = ExternalTools({"exiftool": str(fake)}, env=isolated_env)
    assert not tools.resolve("exiftool").valid


def test_probe_version_reads_first_output_line(tmp_path: Path, fake_exe: FakeExe) -> None:
    tool = fake_exe(tmp_path / "v", "exiftool", output="13.25")
    assert deps.probe_version(tool, ("-ver",)) == "13.25"


def test_probe_version_none_for_missing_file(tmp_path: Path) -> None:
    assert deps.probe_version(tmp_path / "missing.bat", ("-version",)) is None


def test_run_binary_returns_bytes(tmp_path: Path, fake_exe: FakeExe) -> None:
    tool = fake_exe(tmp_path / "b", "ffmpeg", output="raw")
    result = deps.run_binary([str(tool), "-version"], timeout=20)
    assert result.returncode == 0
    assert isinstance(result.stdout, bytes)
    assert b"raw" in result.stdout


def test_ollama_default_url_when_server_answers(isolated_env: dict[str, str]) -> None:
    seen: list[str] = []

    def probe(url: str) -> str | None:
        seen.append(url)
        return "0.5.1" if url == DEFAULT_OLLAMA_URL else None

    location = ExternalTools(env=isolated_env, ollama_probe=probe).resolve("ollama")
    assert location.valid
    assert location.url == DEFAULT_OLLAMA_URL
    assert location.version == "0.5.1"
    assert location.source is ToolSource.DEFAULT


def test_ollama_override_that_is_down_falls_back_to_default(isolated_env: dict[str, str]) -> None:
    def probe(url: str) -> str | None:
        return "0.5.1" if url == DEFAULT_OLLAMA_URL else None

    tools = ExternalTools({"ollama": "http://192.0.2.1:9999"}, env=isolated_env, ollama_probe=probe)
    location = tools.resolve("ollama")
    assert location.valid
    assert location.url == DEFAULT_OLLAMA_URL
    assert any("not responding" in p for p in location.problems)


def test_ollama_env_url_is_used_before_default(isolated_env: dict[str, str]) -> None:
    isolated_env["VARIOUS_TOOLS_OLLAMA_URL"] = "http://gpu-box:11434"
    seen: list[str] = []

    def probe(url: str) -> str | None:
        seen.append(url)
        return "0.6.0" if url == "http://gpu-box:11434" else None

    location = ExternalTools(env=isolated_env, ollama_probe=probe).resolve("ollama")
    assert location.source is ToolSource.ENVIRONMENT
    assert location.url == "http://gpu-box:11434"
    assert seen[0] == "http://gpu-box:11434"


def test_ollama_unreachable_is_disabled(isolated_env: dict[str, str]) -> None:
    location = ExternalTools(env=isolated_env, ollama_probe=lambda _u: None).resolve("ollama")
    assert not location.valid
    assert location.url == DEFAULT_OLLAMA_URL
    assert location.install_hint == "winget install Ollama.Ollama"


def test_probe_ollama_rejects_non_http_urls() -> None:
    assert deps.probe_ollama("file:///etc/passwd") is None
    assert deps.probe_ollama("ftp://example.invalid") is None


def test_probe_ollama_unreachable_host_is_none() -> None:
    # Port 9 (discard) on the loopback address is closed, so the connection fails fast.
    assert deps.probe_ollama("http://127.0.0.1:9") is None


def test_install_hints_use_the_winget_ids() -> None:
    assert install_hint("ffmpeg") == "winget install Gyan.FFmpeg"
    assert install_hint("ffprobe") == "winget install Gyan.FFmpeg"
    assert install_hint("ollama") == "winget install Ollama.Ollama"


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_override_path_matches_regardless_of_case(
    tmp_path: Path, fake_exe: FakeExe, isolated_env: dict[str, str]
) -> None:
    tool = fake_exe(tmp_path / "Tools Dir", "exiftool", output="13.2")
    tools = ExternalTools({"exiftool": str(tool).upper()}, env=isolated_env)
    location = tools.resolve("exiftool")
    assert location.valid
    assert location.source is ToolSource.OVERRIDE


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_known_locations_differing_only_by_case_are_searched_once(
    tmp_path: Path, isolated_env: dict[str, str]
) -> None:
    folder = tmp_path / "Known"
    spec = dataclasses.replace(
        deps.TOOL_SPECS["exiftool"],
        known_locations=(str(folder), str(folder).upper()),
    )
    candidates = ExternalTools(env=isolated_env)._known_candidates(spec)
    # One candidate per PATHEXT extension. The two spellings of the folder must not double them.
    assert len(candidates) == len(deps._windows_path_exts())
    assert len({os.path.normcase(str(c)) for c in candidates}) == len(candidates)


def test_system_tool_falls_back_to_bare_name(
    tmp_path: Path, isolated_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "no-windows-here"))
    assert deps.system_tool("attrib") == "attrib.exe"


def test_unknown_tool_name_is_an_error(isolated_env: dict[str, str]) -> None:
    with pytest.raises(KeyError):
        ExternalTools(env=isolated_env).resolve("notepad")


def test_resolve_all_covers_every_spec(isolated_env: dict[str, str]) -> None:
    result = ExternalTools(env=isolated_env, ollama_probe=lambda _u: None).resolve_all()
    assert set(result) == set(deps.TOOL_SPECS)


def test_ollama_preferred_url_is_tried_before_the_default(isolated_env: dict[str, str]) -> None:
    seen: list[str] = []

    def probe(url: str) -> str | None:
        seen.append(url)
        return "0.7.0" if url == "http://gpu-box:11434" else None

    tools = ExternalTools(env=isolated_env, ollama_probe=probe)
    location = tools.resolve_ollama("http://gpu-box:11434")
    assert location.valid and location.url == "http://gpu-box:11434"
    assert location.source is ToolSource.OVERRIDE
    assert seen == ["http://gpu-box:11434"]
    assert tools.resolve_ollama("http://gpu-box:11434") is location  # cached


def test_ollama_preferred_url_that_is_down_falls_back_and_says_so(
    isolated_env: dict[str, str],
) -> None:
    tools = ExternalTools(
        env=isolated_env,
        ollama_probe=lambda url: "0.5.1" if url == DEFAULT_OLLAMA_URL else None,
    )
    location = tools.resolve_ollama("http://192.0.2.1:9999")
    assert location.valid and location.url == DEFAULT_OLLAMA_URL
    assert any("not responding" in p for p in location.problems)


def test_ollama_preferred_url_equal_to_default_is_the_plain_result(
    isolated_env: dict[str, str],
) -> None:
    tools = ExternalTools(env=isolated_env, ollama_probe=lambda _u: "1")
    assert tools.resolve_ollama(DEFAULT_OLLAMA_URL) is tools.resolve("ollama")
    assert tools.resolve_ollama(None) is tools.resolve("ollama")
