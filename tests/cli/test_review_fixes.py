"""CLI fixes from the review: plan roots, program checks in ``config set``, cloud-only explain,
🔒 locked options, export overwrite, copy to clipboard, one-off program flags on every command, the
README mapping table, and more scan -> apply -> undo round trips. Every tree is generated."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import typer

from various_tools.cli import clipboard, toolcmds
from various_tools.cli.main import app
from various_tools.core import onedrive
from various_tools.core.journal import Journal
from various_tools.core.presets import PresetStore
from various_tools.core.settings import load_settings

if TYPE_CHECKING:
    from tests.cli.conftest import Cli

Snapshot = Callable[[Path], list[tuple[str, bool]]]
FakeExe = Callable[..., Path]
README = Path(__file__).resolve().parents[2] / "README.md"


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


# ----- 4: a plan without roots ----------------------------------------------------------------


@pytest.mark.parametrize("how", ["empty", "missing"])
def test_plan_without_roots_is_refused(
    cli: Cli, empty_tree: Path, snapshot: Snapshot, how: str
) -> None:
    plan = cli.work / "plan.json"
    assert (
        cli.run("empty_folders", "scan", str(empty_tree), "--save-plan", str(plan)).exit_code == 0
    )
    data = json.loads(plan.read_text(encoding="utf-8"))
    if how == "empty":
        data["roots"] = []
    else:
        del data["roots"]
    plan.write_text(json.dumps(data), encoding="utf-8")
    before = snapshot(cli.work)
    result = cli.run("empty_folders", "apply", "--plan", str(plan), "--yes")
    assert result.exit_code == 2, result.output
    assert "no 'roots'" in result.output
    assert snapshot(cli.work) == before
    assert not cli.journal.exists()


# ----- 5: config set validates program paths -------------------------------------------------


def test_config_set_program_path_must_pass_the_version_check(
    cli: Cli, fake_exe: FakeExe, tmp_path: Path
) -> None:
    before = cli.config.read_text(encoding="utf-8")
    missing = cli.run(
        "config", "set", "external_tools.exiftool.path", str(tmp_path / "gone" / "exiftool.exe")
    )
    assert missing.exit_code == 1
    assert "Not saved" in missing.output and "no file" in missing.output
    broken = tmp_path / "broken" / "exiftool.bat"
    broken.parent.mkdir()
    broken.write_text("@echo off\r\nexit /b 3\r\n", encoding="ascii")
    failing = cli.run("config", "set", "external_tools.exiftool.path", str(broken))
    assert failing.exit_code == 1
    assert "did not report a version" in failing.output
    assert cli.config.read_text(encoding="utf-8") == before

    good = fake_exe(tmp_path / "bin", "exiftool", "13.11")
    saved = cli.run("config", "set", "external_tools.exiftool.path", str(good))
    assert saved.exit_code == 0, saved.output
    assert "13.11" in saved.output
    assert load_settings(cli.config).external_tools == {"exiftool": str(good)}


def test_config_set_ollama_address_only_warns_when_unreachable(cli: Cli) -> None:
    result = cli.run("config", "set", "ollama_url", "http://127.0.0.1:9")
    assert result.exit_code == 0, result.output
    assert "warning" in result.output and "did not answer" in result.output
    assert load_settings(cli.config).ollama_url == "http://127.0.0.1:9"
    override = cli.run("config", "set", "external_tools.ollama.url", "http://127.0.0.1:9")
    assert override.exit_code == 0, override.output
    assert "did not answer" in override.output
    assert load_settings(cli.config).external_tools == {"ollama": "http://127.0.0.1:9"}


# ----- 6: explain on a cloud-only file -------------------------------------------------------


@pytest.fixture
def no_downloads(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []
    monkeypatch.setattr(onedrive, "force_download", lambda *a, **k: calls.append(a))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    return calls


def test_explain_skips_a_cloud_only_file(
    cli: Cli,
    make_image: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    no_downloads: list[object],
) -> None:
    local = make_image("local.png", seed=1)
    cloud = make_image("cloud.png", seed=1)
    real = onedrive.get_attributes

    def attributes(path: Path) -> int | None:
        if Path(path).name == "cloud.png":
            return onedrive.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        return real(path)

    monkeypatch.setattr(onedrive, "get_attributes", attributes)
    result = cli.run("duplicates", "explain", str(local), str(cloud))
    assert result.exit_code == 1
    assert "skipped: cloud-only" in result.output
    assert "Traceback" not in result.output
    assert no_downloads == []


def test_explain_reports_a_cloud_only_error_from_the_engine(
    cli: Cli,
    make_image: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    no_downloads: list[object],
) -> None:
    a, b = make_image("a.png", seed=1), make_image("b.png", seed=2)

    def cloud_only(*_args: Any) -> Any:
        raise onedrive.CloudOnlyFileError(b)

    monkeypatch.setattr("various_tools.cli.explain_cmd.explain", cloud_only)
    result = cli.run("duplicates", "explain", str(a), str(b))
    assert result.exit_code == 1
    assert "skipped: cloud-only" in result.output and "b.png" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert no_downloads == []


# ----- 7: locked options ---------------------------------------------------------------------


def test_a_changed_locked_option_asks_and_yes_does_not_answer(
    cli: Cli, empty_tree: Path, snapshot: Snapshot
) -> None:
    plan = cli.work / "plan.json"
    scan = ["empty_folders", "scan", str(empty_tree), "--delete-mode", "permanent"]
    refused = cli.run(*scan, "--save-plan", str(plan))  # nobody to answer
    assert refused.exit_code == 1
    assert "Locked values differ" in refused.output
    assert "--delete-mode" in refused.output and "default: trash" in refused.output
    assert "--allow-locked" in refused.output
    assert not plan.exists()

    declined = cli.run(*scan, "--save-plan", str(plan), input="n\n")
    assert declined.exit_code == 1
    assert not plan.exists()

    accepted = cli.run(*scan, "--save-plan", str(plan), input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert plan.exists()
    assert cli.run(*scan, "--allow-locked", "--json").exit_code == 0

    before = snapshot(cli.work)
    apply_yes = cli.run(
        "empty_folders", "apply", str(empty_tree), "--delete-mode", "permanent", "--yes"
    )
    assert apply_yes.exit_code == 1, "--yes alone must not accept a changed locked value"
    assert "Locked values differ" in apply_yes.output
    assert snapshot(cli.work) == before
    assert not cli.journal.exists()


def test_the_default_of_a_locked_option_needs_no_question(cli: Cli, empty_tree: Path) -> None:
    result = cli.run("empty_folders", "scan", str(empty_tree), "--delete-mode", "trash", "--json")
    assert result.exit_code == 0, result.output
    assert "Locked" not in result.output


def test_locked_values_from_a_preset_ask_too(cli: Cli, empty_tree: Path) -> None:
    save = ["presets", "save", "empty_folders", "perm", "--delete-mode", "permanent"]
    refused = cli.run(*save)
    assert refused.exit_code == 1
    assert "Locked values differ" in refused.output
    assert not cli.presets.exists()
    saved = cli.run(*save, "--allow-locked")
    assert saved.exit_code == 0, saved.output
    assert PresetStore(cli.presets).get("empty_folders", "perm") == {"delete_mode": "permanent"}

    used = cli.run("empty_folders", "scan", str(empty_tree), "--preset", "perm")
    assert used.exit_code == 1
    assert "--delete-mode" in used.output
    ok = cli.run("empty_folders", "scan", str(empty_tree), "--preset", "perm", "--allow-locked")
    assert ok.exit_code == 0, ok.output


def test_locked_thresholds_of_a_preset_that_is_not_complete(cli: Cli) -> None:
    refused = cli.run("presets", "save", "duplicates", "loose", "--phash-max", "9")
    assert refused.exit_code == 1
    assert "--phash-max" in refused.output
    assert cli.run("presets", "save", "duplicates", "tuned", "--phash-max", "2").exit_code == 0


# ----- 11: export never overwrites without --force -------------------------------------------


def test_export_refuses_to_overwrite_without_force(cli: Cli, empty_tree: Path) -> None:
    target = cli.work / "report.csv"
    target.write_text("keep me", encoding="utf-8")
    refused = cli.run("empty_folders", "scan", str(empty_tree), "--export", str(target))
    assert refused.exit_code == 1
    assert "already exists" in refused.output and "--force" in refused.output
    assert "Plan for" not in refused.output, "refused before scanning"
    assert target.read_text(encoding="utf-8") == "keep me"

    forced = cli.run("empty_folders", "scan", str(empty_tree), "--export", str(target), "--force")
    assert forced.exit_code == 0, forced.output
    assert target.read_text(encoding="utf-8-sig").startswith("Selected")


def test_apply_and_the_export_command_refuse_to_overwrite(
    cli: Cli, empty_tree: Path, snapshot: Snapshot
) -> None:
    plan = cli.work / "plan.json"
    cli.run("empty_folders", "scan", str(empty_tree), "--save-plan", str(plan))
    target = cli.work / "done.csv"
    target.write_text("keep me", encoding="utf-8")
    before = snapshot(cli.work)
    applied = cli.run("empty_folders", "apply", str(empty_tree), "--yes", "--export", str(target))
    assert applied.exit_code == 1
    assert snapshot(cli.work) == before, "nothing was applied"

    export = cli.run("export", str(target), "--plan", str(plan))
    assert export.exit_code == 1
    assert target.read_text(encoding="utf-8") == "keep me"
    assert cli.run("export", str(target), "--plan", str(plan), "--force").exit_code == 0
    assert target.read_text(encoding="utf-8-sig").startswith("Selected")


# ----- 12: copy to clipboard -----------------------------------------------------------------


@pytest.fixture
def copied(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The CLI clipboard writes are recorded; the real clipboard is never touched."""
    texts: list[str] = []

    def record(text: str) -> bool:
        texts.append(text)
        return True

    monkeypatch.setattr(clipboard, "set_clipboard_text", record)
    return texts


def _project(cli: Cli) -> Path:
    project = cli.work / "proj"
    project.mkdir()
    (project / "main.py").write_text("print('hello')\n", encoding="utf-8")
    return project


def test_apply_copies_the_written_file_when_asked(cli: Cli, copied: list[str]) -> None:
    project = _project(cli)
    output = cli.work / "export.txt"
    common = ["project_export", "apply", str(project), "--output-path", str(output), "--yes"]
    result = cli.run(*common, "--copy-to-clipboard")
    assert result.exit_code == 0, result.output
    assert copied == [output.read_text(encoding="utf-8")]
    assert "clipboard" in result.output
    assert "print('hello')" in copied[0]

    output.unlink()
    plain = cli.run(*common)
    assert plain.exit_code == 0, plain.output
    assert len(copied) == 1, "no copy unless the option is on"


def test_a_large_written_file_is_not_copied(
    cli: Cli, copied: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clipboard, "MAX_CLIPBOARD_BYTES", 10)
    project = _project(cli)
    output = cli.work / "export.txt"
    result = cli.run(
        "project_export",
        "apply",
        str(project),
        "--output-path",
        str(output),
        "--copy-to-clipboard",
        "--yes",
    )
    assert result.exit_code == 0, result.output
    assert copied == []
    assert "Not copied to the clipboard" in result.output
    assert output.is_file()


def test_clipboard_helpers() -> None:
    from various_tools.core.plan import ActionOutcome, Plan, RunResult

    plan = Plan(tool="x", roots=[], options_json=json.dumps({"copy_to_clipboard": True}))
    assert clipboard.wants_clipboard(plan)
    assert not clipboard.wants_clipboard(Plan(tool="x", roots=[], options_json='{"a": 1}'))
    assert not clipboard.wants_clipboard(
        Plan(tool="x", roots=[], options_json='{"copy_to_clipboard": "yes"}')
    )
    result = RunResult(
        run_id=1,
        status="done",
        outcomes=[
            ActionOutcome(1, "write", "failed", None, "a.txt"),
            ActionOutcome(2, "write", "done", None, "b.txt"),
        ],
    )
    assert clipboard.written_file(result) == Path("b.txt")
    assert clipboard.written_file(RunResult(run_id=2, status="done")) is None


# ----- 13: one-off program flags on every command --------------------------------------------


def test_program_flags_work_after_the_command(
    cli: Cli, fake_exe: FakeExe, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty_tree: Path
) -> None:
    before = cli.config.read_text(encoding="utf-8")
    bin_dir = tmp_path / "bin"
    flags = ["--ollama-url", "http://127.0.0.1:9"]
    for program in ("exiftool", "ffmpeg", "ffprobe"):
        flags += [f"--{program}", str(fake_exe(bin_dir, program, f"{program} 4.56"))]

    doctor = cli.run("doctor", *flags)
    assert doctor.exit_code == 0, doctor.output
    exif_line = next(line for line in doctor.output.splitlines() if "exiftool" in line)
    assert "4.56" in exif_line and "override" in exif_line

    seen: list[Any] = []
    real_run_job = toolcmds.run_job

    def spy(state: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(state)
        return real_run_job(state, *args, **kwargs)

    monkeypatch.setattr(toolcmds, "run_job", spy)
    scan = cli.run("empty_folders", "scan", str(empty_tree), *flags, "--json")
    assert scan.exit_code == 0, scan.output
    assert seen[-1].one_off["exiftool"] == str(bin_dir / "exiftool.bat")
    assert seen[-1].ollama_url == "http://127.0.0.1:9"
    # the command's flag wins over the global one
    other = fake_exe(tmp_path / "other", "exiftool", "exiftool 7.0")
    both = cli.run(
        "--exiftool", str(other), "empty_folders", "scan", str(empty_tree), *flags, "--json"
    )
    assert both.exit_code == 0, both.output
    assert seen[-1].one_off["exiftool"] == str(bin_dir / "exiftool.bat")
    assert cli.config.read_text(encoding="utf-8") == before, "nothing is saved"

    for command in ("scan", "apply"):
        text = " ".join(cli.run("empty_folders", command, "--help").output.split())
        for flag in ("--exiftool", "--ffmpeg", "--ffprobe", "--ollama-url"):
            assert flag in text


# ----- 16: the README mapping table ----------------------------------------------------------


def _command_options(command: Any) -> set[str]:
    names: set[str] = set()
    for param in command.params:
        names.update(param.opts)
        names.update(getattr(param, "secondary_opts", []))
    return names


def _readme_rows() -> list[tuple[str, str]]:
    text = README.read_text(encoding="utf-8")
    section = text.split("### Old script to new command", 1)[1].split("\n## ", 1)[0]
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        rows.append((cells[0], cells[1]))
    return rows


def test_every_readme_flag_exists_on_its_command() -> None:
    root: Any = typer.main.get_command(app)
    tools = {name for name, cmd in root.commands.items() if hasattr(cmd, "commands")} - {
        "config",
        "presets",
    }
    rows = _readme_rows()
    assert len(rows) >= 8, "the mapping table was not found"
    checked = 0
    for old, new in rows:
        mentioned = [t for t in tools if f"various-tools {t} " in new or f"`{t}`" in new]
        assert mentioned, f"no tool command in README row: {new[:60]}"
        options: set[str] = _command_options(root)  # global options are valid too
        for tool_id in mentioned:
            for command in root.commands[tool_id].commands.values():
                options |= _command_options(command)
        # Sentences saying an old flag has no equivalent, and "for `--move`" (the old flag a
        # command replaces), name flags that are not meant to exist.
        sentences = [s for s in re.split(r"\.\s", new) if "no equivalent" not in s]
        kept = re.sub(r"for `--[\w-]+`", "", " ".join(sentences))
        old_flags = set(re.findall(r"--[A-Za-z][\w-]*", old))
        for flag in re.findall(r"--[A-Za-z][\w-]*", kept):
            if flag in old_flags and flag not in options:
                continue  # an old script's own flag, quoted for comparison
            assert flag in options, f"README shows {flag} with {mentioned}, but it does not exist"
            checked += 1
    assert checked > 40


# ----- 16: scan -> apply -> undo for sort_by_type and organize_by_date -----------------------


def test_sort_by_type_scan_apply_undo(cli: Cli, snapshot: Snapshot) -> None:
    folder = cli.work / "downloads"
    folder.mkdir()
    (folder / "report.pdf").write_text("pdf", encoding="utf-8")
    (folder / "notes.txt").write_text("txt", encoding="utf-8")
    before = snapshot(folder)
    plan = cli.work / "sort.json"
    scan = cli.run("sort_by_type", "scan", str(folder), "--save-plan", str(plan))
    assert scan.exit_code == 0, scan.output
    assert snapshot(folder) == before, "scan changes nothing"

    applied = cli.run("sort_by_type", "apply", "--plan", str(plan), "--yes")
    assert applied.exit_code == 0, applied.output
    assert (folder / "Documents" / "PDF" / "report.pdf").is_file()
    assert not (folder / "report.pdf").exists()

    undone = cli.run("undo", "1", "--yes")
    assert undone.exit_code == 0, undone.output
    assert (folder / "report.pdf").read_text(encoding="utf-8") == "pdf"
    assert (folder / "notes.txt").read_text(encoding="utf-8") == "txt"
    with Journal(cli.journal) as journal:
        run = journal.run(1)
        assert run is not None and run.status == "undone"


def test_organize_by_date_scan_apply_undo(cli: Cli, make_image: Callable[..., Path]) -> None:
    source = cli.work / "camera"
    photo = make_image(str(source.relative_to(cli.root) / "IMG_0001.jpg"))
    stamp = datetime(2021, 5, 3, 12, 0).timestamp()
    os.utime(photo, (stamp, stamp))
    dest = cli.work / "library"
    options = ["--dest-root", str(dest), "--date-priority", "modified"]
    scan = cli.run("organize_by_date", "scan", str(source), *options)
    assert scan.exit_code == 0, scan.output
    assert photo.is_file() and not dest.exists()

    applied = cli.run("organize_by_date", "apply", str(source), *options, "--yes")
    assert applied.exit_code == 0, applied.output
    moved = list(dest.rglob("IMG_0001.jpg"))
    assert len(moved) == 1 and "2021" in str(moved[0])
    assert not photo.exists()

    undone = cli.run("undo", "1", "--yes")
    assert undone.exit_code == 0, undone.output
    assert photo.is_file()
    assert list(dest.rglob("IMG_0001.jpg")) == []
