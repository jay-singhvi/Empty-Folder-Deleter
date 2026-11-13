"""Checksum and verify: manifest format, create then verify, tamper detection, and safety rules.

Every tree is generated under ``tmp_path``. Cloud-only files are real files that carry the
Windows OFFLINE attribute, and ``Path.open`` is watched, so a test fails if one is ever opened.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from various_tools.core import onedrive
from various_tools.core.context import RunContext
from various_tools.core.deps import ExternalTools
from various_tools.core.journal import Journal, undo_run
from various_tools.core.plan import ActionKind, Plan, PlannedAction, RunResult
from various_tools.core.settings import Settings
from various_tools.tools.checksum import TOOL, ChecksumOptions
from various_tools.tools.checksum import manifest as mf
from various_tools.tools.checksum import tool as checksum_tool

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows file attributes")


# ----- helpers --------------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    j = Journal(tmp_path / "journal.sqlite")
    yield j
    j.close()


def _ctx(tmp_path: Path, journal: Journal | None = None, **settings: object) -> RunContext:
    base: dict[str, object] = {"onedrive_root": tmp_path}
    base.update(settings)
    return RunContext(
        settings=Settings.model_validate(base),
        deps=ExternalTools(ollama_probe=lambda _u: None),
        journal=journal,
    )


def _opts(**values: object) -> ChecksumOptions:
    return ChecksumOptions.model_validate(values)


def _tree(root: Path) -> Path:
    """A small tree: two top files, a nested file, and a deeper one."""
    files = {
        "a.txt": b"alpha",
        "b.bin": bytes(range(256)) * 40,
        "sub/c.txt": b"charlie",
        "sub/deeper/d.txt": b"delta",
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(root: Path) -> dict[str, tuple[bool, int, int]]:
    """Every entry (folders too) with its size and modified time, for before/after checks."""
    return {
        p.relative_to(root).as_posix(): (p.is_dir(), p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
    }


def _create(
    tmp_path: Path, journal: Journal, root: Path, **values: object
) -> tuple[Plan, RunResult]:
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(roots=[root], **values), ctx)
    return plan, TOOL.apply(plan, ctx)


def _verify(tmp_path: Path, root: Path, **values: object) -> Plan:
    return TOOL.scan(_opts(mode="verify", roots=[root], **values), _ctx(tmp_path))


def _lines(plan: Plan, prefix: str) -> list[str]:
    return [n for n in plan.notes if n.startswith(prefix)]


def _set_attributes(path: Path, attributes: int) -> None:
    ctypes.WinDLL("kernel32", use_last_error=True).SetFileAttributesW(str(path), attributes)


@pytest.fixture
def watch_opens(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], list[str]]:
    """Record the file names opened through ``Path.open`` whose name is in ``names``."""
    real_open = Path.open
    seen: list[str] = []
    names: set[str] = set()

    def spy(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name in names:
            seen.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)

    def watch(name: str) -> list[str]:
        names.add(name)
        return seen

    return watch


# ----- options and registration ---------------------------------------------------------------


def test_defaults_and_choices() -> None:
    opts = ChecksumOptions()
    assert opts.mode == "create" and opts.algorithm == "sha256"
    assert opts.recursive is True and opts.include_hidden is False
    assert opts.manifest_name is None and opts.unreadable_files == "fail"
    assert opts.report_extra is True


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "repair"},
        {"algorithm": "crc32"},
        {"manifest_name": "sub/SUMS.txt"},
        {"manifest_name": "..\\SUMS.txt"},
        {"manifest_name": "bad:name"},
        {"manifest_name": "ends.with.dot."},
        {"read_chunk_kb": 0},
        {"read_chunk_kb": 70000},
        {"unreadable_files": "ignore"},
        {"surprise": True},
    ],
)
def test_bad_options_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ChecksumOptions.model_validate(bad)


def test_a_blank_manifest_name_means_the_default() -> None:
    assert ChecksumOptions(manifest_name="  ").manifest_name is None


def test_default_manifest_name_follows_the_algorithm(tmp_path: Path) -> None:
    root = _tree(tmp_path / "data")
    for algorithm, name in (("sha256", "SHA256SUMS.txt"), ("md5", "MD5SUMS.txt")):
        plan = TOOL.scan(_opts(roots=[root], algorithm=algorithm), _ctx(tmp_path))
        assert plan.actions[0].dst == root / name


def test_the_tool_contract() -> None:
    assert TOOL.id == "checksum" and TOOL.category == "Developer"
    assert TOOL.Options is ChecksumOptions


# ----- scan (create) --------------------------------------------------------------------------


def test_scan_plans_one_write_action_and_changes_nothing(tmp_path: Path) -> None:
    root = _tree(tmp_path / "data")
    before = _snapshot(root)
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path))
    assert _snapshot(root) == before
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.kind is ActionKind.WRITE
    assert action.src is None and action.dst == root / "SHA256SUMS.txt"
    assert action.selected is True
    assert "4 file(s)" in action.reason
    assert plan.notes[0].startswith("Create: 1 manifest(s) ready to write for 4 file(s)")
    assert plan.roots == [root]


def test_scan_does_not_read_any_file(
    tmp_path: Path, watch_opens: Callable[[str], list[str]]
) -> None:
    root = _tree(tmp_path / "data")
    opened = watch_opens("a.txt")
    TOOL.scan(_opts(roots=[root]), _ctx(tmp_path))
    assert opened == []


def test_no_folder_chosen_and_empty_folder(tmp_path: Path) -> None:
    assert "Choose at least one folder." in TOOL.scan(_opts(), _ctx(tmp_path)).notes
    empty = tmp_path / "empty"
    empty.mkdir()
    plan = TOOL.scan(_opts(roots=[empty]), _ctx(tmp_path))
    assert plan.actions == []
    assert any("No files to list" in n for n in plan.notes)


def test_two_folders_get_two_manifests(tmp_path: Path) -> None:
    one = _tree(tmp_path / "one")
    two = _tree(tmp_path / "two")
    plan = TOOL.scan(_opts(roots=[one, two, one]), _ctx(tmp_path))
    assert [a.dst for a in plan.actions] == [one / "SHA256SUMS.txt", two / "SHA256SUMS.txt"]


def test_existing_manifest_is_unselected_with_a_note(tmp_path: Path) -> None:
    root = _tree(tmp_path / "data")
    (root / "SHA256SUMS.txt").write_text("keep me", encoding="utf-8")
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path))
    assert plan.actions[0].selected is False
    assert "already exists" in plan.actions[0].reason
    assert any("never replaced" in n for n in plan.notes)
    assert plan.notes[0].startswith("Create: 0 manifest(s)")


def test_protected_root_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _tree(tmp_path / "profile")
    monkeypatch.setenv("USERPROFILE", str(profile))
    plan = TOOL.scan(_opts(roots=[profile]), _ctx(tmp_path))
    assert plan.actions == [] and plan.roots == []
    assert any("user profile folder" in n for n in plan.notes)


def test_protected_folders_come_from_the_settings(tmp_path: Path) -> None:
    root = _tree(tmp_path / "Guarded")
    refused = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, protected_folders=[str(root)]))
    assert refused.actions == [] and any("Protected folder refused" in n for n in refused.notes)


def test_a_missing_folder_is_refused(tmp_path: Path) -> None:
    plan = TOOL.scan(_opts(roots=[tmp_path / "nope"]), _ctx(tmp_path))
    assert plan.actions == [] and any("Not a folder" in n for n in plan.notes)


def test_manifest_file_is_ignored_when_creating(tmp_path: Path) -> None:
    root = _tree(tmp_path / "data")
    plan = TOOL.scan(_opts(roots=[root], manifest_file=tmp_path / "x.txt"), _ctx(tmp_path))
    assert len(plan.actions) == 1 and any("only used when verifying" in n for n in plan.notes)


# ----- apply (create) -------------------------------------------------------------------------


def test_apply_writes_a_sha256sum_compatible_manifest(tmp_path: Path, journal: Journal) -> None:
    root = _tree(tmp_path / "data")
    _plan, result = _create(tmp_path, journal, root)
    assert result.status == "done" and result.count("done") == 1
    manifest = root / "SHA256SUMS.txt"
    raw = manifest.read_bytes()
    assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf") and raw.endswith(b"\n")
    lines = raw.decode("utf-8").splitlines()
    assert lines == [
        f"{_sha(b'alpha')}  a.txt",
        f"{_sha(bytes(range(256)) * 40)}  b.bin",
        f"{_sha(b'charlie')}  sub/c.txt",
        f"{_sha(b'delta')}  sub/deeper/d.txt",
    ]
    assert "wrote 4 entries" in result.outcomes[0].detail


def test_apply_journals_the_write_and_undo_leaves_it_for_manual_removal(
    tmp_path: Path, journal: Journal
) -> None:
    root = _tree(tmp_path / "data")
    _plan, result = _create(tmp_path, journal, root)
    actions = journal.actions(result.run_id)
    assert [(a.kind, a.status) for a in actions] == [("write", "done")]
    assert actions[0].dst == str(root / "SHA256SUMS.txt") and actions[0].src is None
    report = undo_run(journal, result.run_id)
    assert report.reverted == 0 and any("not reversible" in s for s in report.skipped)
    assert (root / "SHA256SUMS.txt").exists()


def test_apply_hashes_at_apply_time(tmp_path: Path, journal: Journal) -> None:
    root = _tree(tmp_path / "data")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(roots=[root]), ctx)
    (root / "a.txt").write_bytes(b"changed after the scan")
    (root / "new.txt").write_bytes(b"added after the scan")
    TOOL.apply(plan, ctx)
    text = (root / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert f"{_sha(b'changed after the scan')}  a.txt" in text
    assert "new.txt" in text


def test_the_manifest_never_lists_itself_and_hidden_files_are_left_out(
    tmp_path: Path, journal: Journal
) -> None:
    root = _tree(tmp_path / "data")
    (root / ".secret").write_bytes(b"x")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_bytes(b"y")
    _create(tmp_path, journal, root)
    names = [ln.split("  ", 1)[1] for ln in (root / "SHA256SUMS.txt").read_text().splitlines()]
    assert "SHA256SUMS.txt" not in names
    assert not any(n.startswith(".") for n in names) and ".git/config" not in names


@windows_only
def test_windows_hidden_files_and_folders_follow_the_option(
    tmp_path: Path, journal: Journal
) -> None:
    root = _tree(tmp_path / "data")
    _set_attributes(root / "a.txt", 0x02)  # hidden
    _set_attributes(root / "sub", 0x02)  # a hidden folder hides what is inside it
    try:
        plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path))
        assert "1 file(s)" in plan.actions[0].reason  # only b.bin
        plan = TOOL.scan(_opts(roots=[root], include_hidden=True), _ctx(tmp_path))
        assert "4 file(s)" in plan.actions[0].reason
    finally:
        _set_attributes(root / "a.txt", 0x80)
        _set_attributes(root / "sub", 0x10)


def test_include_exclude_and_recursive_options(tmp_path: Path, journal: Journal) -> None:
    root = _tree(tmp_path / "data")
    _create(tmp_path, journal, root, recursive=False)
    flat = (root / "SHA256SUMS.txt").read_text().splitlines()
    assert [ln.split("  ")[1] for ln in flat] == ["a.txt", "b.bin"]
    (root / "SHA256SUMS.txt").unlink()

    _create(tmp_path, journal, root, include_globs=["*.txt"], exclude_globs=["deeper"])
    names = [ln.split("  ")[1] for ln in (root / "SHA256SUMS.txt").read_text().splitlines()]
    assert names == ["a.txt", "sub/c.txt"]


def test_other_algorithms_and_a_custom_name(tmp_path: Path, journal: Journal) -> None:
    root = _tree(tmp_path / "data")
    _create(tmp_path, journal, root, algorithm="md5", manifest_name="sums.md5")
    first = (root / "sums.md5").read_text().splitlines()[0]
    assert first == f"{hashlib.md5(b'alpha').hexdigest()}  a.txt"  # noqa: S324
    assert (
        _verify(tmp_path, root, algorithm="md5", manifest_name="sums.md5")
        .notes[0]
        .startswith("Verify: OK - 4 OK")
    )


def test_manifest_is_never_overwritten_at_apply(tmp_path: Path, journal: Journal) -> None:
    root = _tree(tmp_path / "data")
    (root / "SHA256SUMS.txt").write_text("keep me", encoding="utf-8")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(roots=[root]), ctx)
    plan.actions[0].selected = True  # someone ticks it by hand
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1 and "already exists" in result.outcomes[0].detail
    assert (root / "SHA256SUMS.txt").read_text(encoding="utf-8") == "keep me"
    assert sorted(p.name for p in root.iterdir() if p.is_file()) == [
        "SHA256SUMS.txt",
        "a.txt",
        "b.bin",
    ]


def test_manifest_created_after_the_scan_is_not_replaced(tmp_path: Path, journal: Journal) -> None:
    root = _tree(tmp_path / "data")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(roots=[root]), ctx)
    (root / "SHA256SUMS.txt").write_text("someone else wrote this", encoding="utf-8")
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1
    assert (root / "SHA256SUMS.txt").read_text(encoding="utf-8") == "someone else wrote this"


def test_a_late_file_at_the_rename_step_is_not_replaced_and_no_temp_file_stays(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "data")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(roots=[root]), ctx)
    real_rename = checksum_tool.rename_no_replace

    def late(source: Path, destination: Path) -> None:
        destination.write_bytes(b"late")
        real_rename(source, destination)

    monkeypatch.setattr(checksum_tool, "rename_no_replace", late)
    result = TOOL.apply(plan, ctx)
    assert result.count("failed") == 1
    assert (root / "SHA256SUMS.txt").read_bytes() == b"late"
    assert not [p for p in root.iterdir() if p.suffix == ".tmp"]


def test_apply_refuses_a_manifest_outside_the_root_or_not_directly_in_it(
    tmp_path: Path, journal: Journal
) -> None:
    root = _tree(tmp_path / "data")
    outside = tmp_path / "outside.txt"
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(roots=[root]), ctx)
    forged = Plan(
        tool=plan.tool,
        roots=plan.roots,
        options_json=plan.options_json,
        actions=[
            PlannedAction(kind=ActionKind.WRITE, dst=outside, reason="forged"),
            PlannedAction(kind=ActionKind.WRITE, dst=root / "sub" / "SUMS.txt", reason="forged"),
        ],
    )
    result = TOOL.apply(forged, ctx)
    assert result.count("failed") == 2 and result.count("done") == 0
    assert "outside the selected folders" in result.outcomes[0].detail
    assert "directly in the selected folder" in result.outcomes[1].detail
    assert not outside.exists() and not (root / "sub" / "SUMS.txt").exists()


def test_apply_refuses_a_protected_root_added_to_the_settings_later(
    tmp_path: Path, journal: Journal
) -> None:
    root = _tree(tmp_path / "data")
    plan = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path, journal))
    guarded = _ctx(tmp_path, journal, protected_folders=[str(root)])
    result = TOOL.apply(plan, guarded)
    assert result.count("failed") == 1 and "Protected folder refused" in result.outcomes[0].detail
    assert not (root / "SHA256SUMS.txt").exists()


def test_an_unreadable_file_stops_the_create_by_default(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "data")
    real = mf.file_digest

    def flaky(path: Path, algorithm: str, chunk_size: int) -> str:
        if path.name == "b.bin":
            raise PermissionError(13, "Permission denied")
        return real(path, algorithm, chunk_size)

    monkeypatch.setattr(mf, "file_digest", flaky)
    _plan, result = _create(tmp_path, journal, root)
    assert result.count("failed") == 1 and "could not read b.bin" in result.outcomes[0].detail
    assert not (root / "SHA256SUMS.txt").exists()
    assert not [p for p in root.iterdir() if p.suffix == ".tmp"]


def test_skip_leaves_an_unreadable_file_out_and_says_so(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "data")
    real = mf.file_digest

    def flaky(path: Path, algorithm: str, chunk_size: int) -> str:
        if path.name == "b.bin":
            raise PermissionError(13, "Permission denied")
        return real(path, algorithm, chunk_size)

    monkeypatch.setattr(mf, "file_digest", flaky)
    _plan, result = _create(tmp_path, journal, root, unreadable_files="skip")
    assert result.count("done") == 1
    assert "left out 1 unreadable file(s)" in result.outcomes[0].detail
    assert "b.bin" not in (root / "SHA256SUMS.txt").read_text()


def test_the_read_size_never_changes_the_result(tmp_path: Path, journal: Journal) -> None:
    root = _tree(tmp_path / "data")
    _create(tmp_path, journal, root, read_chunk_kb=4)
    small = (root / "SHA256SUMS.txt").read_bytes()
    (root / "SHA256SUMS.txt").unlink()
    _create(tmp_path, journal, root, read_chunk_kb=65536)
    assert (root / "SHA256SUMS.txt").read_bytes() == small


def test_cancelling_during_apply_writes_nothing(tmp_path: Path, journal: Journal) -> None:
    root = _tree(tmp_path / "data")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(roots=[root]), ctx)
    ctx.cancel()
    result = TOOL.apply(plan, ctx)
    assert result.status == "cancelled" and not (root / "SHA256SUMS.txt").exists()


# ----- verify ---------------------------------------------------------------------------------


def test_create_then_verify_round_trip(tmp_path: Path, journal: Journal) -> None:
    root = _tree(tmp_path / "data")
    _create(tmp_path, journal, root)
    before = _snapshot(root)
    plan = _verify(tmp_path, root)
    assert _snapshot(root) == before  # verify is read-only
    assert plan.actions == []
    assert plan.notes[0] == (
        "Verify: OK - 4 OK, 0 mismatched, 0 missing, 0 unreadable, 0 skipped (cloud-only), "
        "0 extra, 0 invalid line(s), 0 manifest(s) not usable."
    )
    assert plan.notes[1].startswith(f"root: {root} (manifest SHA256SUMS.txt, 4 entries)")
    assert len(plan.notes) == 2


def test_verify_reports_a_changed_a_missing_and_an_extra_file(
    tmp_path: Path, journal: Journal
) -> None:
    root = _tree(tmp_path / "data")
    _create(tmp_path, journal, root)
    (root / "a.txt").write_bytes(b"tampered")
    (root / "sub" / "deeper" / "d.txt").unlink()
    (root / "sub" / "new.txt").write_bytes(b"surprise")
    plan = _verify(tmp_path, root)
    assert plan.notes[0].startswith("Verify: PROBLEMS FOUND - 2 OK, 1 mismatched, 1 missing")
    assert "1 extra" in plan.notes[0]
    assert _lines(plan, "mismatch: ") == ["mismatch: a.txt"]
    assert _lines(plan, "missing: ") == ["missing: sub/deeper/d.txt"]
    assert _lines(plan, "extra: ") == ["extra: sub/new.txt"]


def test_extra_files_do_not_fail_the_verify_and_can_be_switched_off(
    tmp_path: Path, journal: Journal
) -> None:
    root = _tree(tmp_path / "data")
    _create(tmp_path, journal, root)
    (root / "new.txt").write_bytes(b"x")
    plan = _verify(tmp_path, root)
    assert plan.notes[0].startswith("Verify: OK - 4 OK") and "1 extra" in plan.notes[0]
    quiet = _verify(tmp_path, root, report_extra=False)
    assert "0 extra" in quiet.notes[0] and _lines(quiet, "extra: ") == []


def test_verify_without_a_manifest(tmp_path: Path) -> None:
    root = _tree(tmp_path / "data")
    plan = _verify(tmp_path, root)
    assert (
        plan.notes[0].startswith("Verify: PROBLEMS FOUND")
        and "1 manifest(s) not usable" in plan.notes[0]
    )
    assert any(n.startswith("Manifest not found: ") for n in plan.notes)


def test_verify_a_copy_against_a_manifest_from_the_original(
    tmp_path: Path, journal: Journal
) -> None:
    original = _tree(tmp_path / "original")
    _create(tmp_path, journal, original)
    copy = _tree(tmp_path / "backup")
    manifest = original / "SHA256SUMS.txt"
    good = _verify(tmp_path, copy, manifest_file=manifest)
    assert good.notes[0].startswith("Verify: OK - 4 OK")
    (copy / "sub" / "c.txt").write_bytes(b"bit rot")
    bad = _verify(tmp_path, copy, manifest_file=manifest)
    assert _lines(bad, "mismatch: ") == ["mismatch: sub/c.txt"]


def test_manifest_file_needs_exactly_one_folder(tmp_path: Path) -> None:
    one, two = _tree(tmp_path / "one"), _tree(tmp_path / "two")
    plan = TOOL.scan(
        _opts(mode="verify", roots=[one, two], manifest_file=tmp_path / "m.txt"), _ctx(tmp_path)
    )
    assert plan.notes[-1] == "A manifest file can be checked against one folder at a time."


def test_invalid_lines_are_reported_and_never_read(tmp_path: Path) -> None:
    root = _tree(tmp_path / "data")
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"outside the root")
    digest = _sha(b"outside the root")
    (root / "SHA256SUMS.txt").write_text(
        "\n".join(
            [
                f"{_sha(b'alpha')}  a.txt",
                f"{digest}  ../secret.txt",
                f"{digest}  sub/../../secret.txt",
                f"{digest}  {secret}",
                f"{digest}  /etc/passwd",
                f"{digest}  \\\\server\\share\\x",
                f"{digest}  C:\\Windows\\win.ini",
                f"{digest}  stream:name",
                "deadbeef  short.txt",
                "this is not a manifest line",
                "# a comment",
                "",
            ]
        ),
        encoding="utf-8",
    )
    plan = _verify(tmp_path, root)
    invalid = _lines(plan, "invalid: line ")
    assert [n.split(":")[1].strip() for n in invalid] == [
        "line 2",
        "line 3",
        "line 4",
        "line 5",
        "line 6",
        "line 7",
        "line 8",
        "line 9",
        "line 10",
    ]
    assert "9 invalid line(s)" in plan.notes[0] and "PROBLEMS FOUND" in plan.notes[0]
    assert "1 OK" in plan.notes[0]
    assert _lines(plan, "mismatch: ") == []


def test_wrong_algorithm_makes_every_line_invalid(tmp_path: Path, journal: Journal) -> None:
    root = _tree(tmp_path / "data")
    _create(tmp_path, journal, root)
    plan = _verify(tmp_path, root, algorithm="md5", manifest_name="SHA256SUMS.txt")
    assert "4 invalid line(s)" in plan.notes[0]
    assert "not 32 hex characters (md5)" in _lines(plan, "invalid: ")[0]


def test_manifest_reading_is_tolerant_of_other_writers() -> None:
    digest = _sha(b"x")
    raw = "\ufeff" + "\r\n".join(
        [
            f"{digest.upper()} *bin/mode.txt",
            f"{digest}  win\\style\\path.txt",
            f"{digest}  ./dot/slash.txt",
            "",
        ]
    )
    entries, invalid = mf.parse(mf.decode(raw.encode("utf-8")), "sha256")
    assert invalid == []
    assert [e.path for e in entries] == ["bin/mode.txt", "win/style/path.txt", "dot/slash.txt"]
    assert all(e.digest == digest for e in entries)
    with pytest.raises(ValueError, match="UTF-8"):
        mf.decode("é".encode("cp1252"))


def test_verify_never_follows_a_path_through_a_junction(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are a Windows feature")
    root = _tree(tmp_path / "data")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_bytes(b"secret")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)],
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("this account cannot create junctions")
    try:
        (root / "SHA256SUMS.txt").write_text(f"{_sha(b'secret')}  link/s.txt\n", encoding="utf-8")
        plan = _verify(tmp_path, root)
        assert _lines(plan, "unreadable: link/s.txt (junction or symlink")
        assert not any("s.txt" in n for n in _lines(plan, "extra: "))  # nothing behind the link
        assert "0 OK" in plan.notes[0]
        # Creating never lists what is behind the junction either.
        (root / "SHA256SUMS.txt").unlink()
        listing = TOOL.scan(_opts(roots=[root]), _ctx(tmp_path))
        assert "4 file(s)" in listing.actions[0].reason
    finally:
        os.rmdir(root / "link")  # removes the junction only, never its target
    assert (outside / "s.txt").exists()


# ----- cloud-only files -----------------------------------------------------------------------


@windows_only
def test_cloud_only_files_are_skipped_never_opened_when_creating(
    tmp_path: Path, journal: Journal, watch_opens: Callable[[str], list[str]]
) -> None:
    root = _tree(tmp_path / "data")
    cloud = root / "b.bin"
    _set_attributes(cloud, 0x1000)  # OFFLINE: the content is not on this device
    opened = watch_opens("b.bin")
    try:
        ctx = _ctx(tmp_path, journal)
        plan = TOOL.scan(_opts(roots=[root]), ctx)
        assert "1 cloud-only file(s) skipped" in plan.notes[0]
        assert "skipped: cloud-only: b.bin" in plan.notes
        assert "3 file(s)" in plan.actions[0].reason
        result = TOOL.apply(plan, ctx)
        assert "skipped 1 cloud-only file(s)" in result.outcomes[0].detail
        assert "b.bin" not in (root / "SHA256SUMS.txt").read_text()
        assert opened == []
    finally:
        _set_attributes(cloud, 0x80)


@windows_only
def test_cloud_only_files_are_skipped_never_opened_when_verifying(
    tmp_path: Path, journal: Journal, watch_opens: Callable[[str], list[str]]
) -> None:
    root = _tree(tmp_path / "data")
    _create(tmp_path, journal, root)
    cloud = root / "b.bin"
    _set_attributes(cloud, 0x1000)
    opened = watch_opens("b.bin")
    try:
        plan = _verify(tmp_path, root)
        assert plan.notes[0].startswith("Verify: PROBLEMS FOUND - 3 OK")
        assert "1 skipped (cloud-only)" in plan.notes[0]
        assert _lines(plan, "skipped: cloud-only: ") == ["skipped: cloud-only: b.bin"]
        assert opened == []
    finally:
        _set_attributes(cloud, 0x80)


def test_a_file_that_became_cloud_only_after_the_scan_is_never_hashed(
    tmp_path: Path, journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "data")
    ctx = _ctx(tmp_path, journal)
    plan = TOOL.scan(_opts(roots=[root]), ctx)
    real_attributes = onedrive.get_attributes

    def attributes(path: Path) -> int | None:
        if path.name == "a.txt":
            return onedrive.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        return real_attributes(path)

    def must_not_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cloud-only file was read")

    monkeypatch.setattr(onedrive, "get_attributes", attributes)
    real_open = Path.open

    def guarded(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == "a.txt":
            must_not_read()
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    result = TOOL.apply(plan, ctx)
    assert result.count("done") == 1
    assert "skipped 1 cloud-only file(s)" in result.outcomes[0].detail
    assert "a.txt" not in (root / "SHA256SUMS.txt").read_text()


def test_a_junction_as_the_selected_folder_is_refused(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are a Windows feature")
    outside = _tree(tmp_path / "outside")
    link = tmp_path / "linkroot"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False
    )
    if made.returncode != 0:
        pytest.skip("this account cannot create junctions")
    try:
        plan = TOOL.scan(_opts(roots=[link]), _ctx(tmp_path))
        assert plan.actions == [] and plan.roots == []
    finally:
        os.rmdir(link)  # removes the junction only, never its target
    assert not (outside / "SHA256SUMS.txt").exists()
