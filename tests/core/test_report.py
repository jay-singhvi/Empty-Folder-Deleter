"""Reports: CSV, Excel, HTML, the old review list and the old CSV log, for plans, runs and results.

Everything is generated under ``tmp_path``. The cloud-only tests make ``open_image`` fail, so they
prove that no image is opened for a file that is not on the device (rule 3).
"""

from __future__ import annotations

import csv
import html
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

from various_tools.core import report
from various_tools.core.journal import ActionRecord, RunRecord
from various_tools.core.media import thumbs
from various_tools.core.plan import ActionKind, ActionOutcome, Plan, PlannedAction, RunResult

ImageMaker = Callable[..., Path]


def read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def simple_plan(tmp_path: Path) -> Plan:
    """A rename-style plan with one real file, one missing file and one folder."""
    folder = tmp_path / "root"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("12345")
    return Plan(
        tool="rename",
        roots=[folder],
        actions=[
            PlannedAction(ActionKind.RENAME, folder / "a.txt", folder / "b.txt", reason="why <b>"),
            PlannedAction(ActionKind.TRASH, folder / "gone.txt", reason="missing", selected=False),
            PlannedAction(ActionKind.TRASH, folder / "sub", reason="empty folder"),
        ],
        notes=["a note & more"],
        options_json=json.dumps({"match": "*.txt"}),
        created="2026-01-02T03:04:05+00:00",
    )


def duplicates_plan(make_image: ImageMaker, tmp_path: Path) -> tuple[Plan, Path, Path, Path]:
    keeper = make_image("keep.jpg", seed=1)
    dup_a = make_image("dup_a.jpg", seed=2)
    dup_b = make_image("dup_b.jpg", seed=3)
    folder = tmp_path / "duplicate"
    plan = Plan(
        tool="duplicates",
        roots=[tmp_path],
        actions=[
            PlannedAction(
                ActionKind.MOVE,
                dup_a,
                folder / dup_a.name,
                reason="identical bytes (SHA-256); keeps keep.jpg",
                group_id=str(keeper),
            ),
            PlannedAction(
                ActionKind.TRASH,
                dup_b,
                reason="pHash 1/2, dHash 2/4, block diff 0.50/3.0, taken ? vs ?; keeps keep.jpg",
                group_id=str(keeper),
            ),
        ],
        notes=["Duplicate finder: 1 exact, 1 visual."],
    )
    return plan, keeper, dup_a, dup_b


def sample_run() -> tuple[RunRecord, list[ActionRecord]]:
    run = RunRecord(7, "rename", '{"x": 1}', "2026-01-01T10:00:00+00:00", None, "partial")
    actions = [
        ActionRecord(1, 7, 1, "move", "C:/a/x.jpg", "C:/b/x.jpg", "dup", "done", None, "t1"),
        ActionRecord(2, 7, 2, "trash", "C:/a/y.jpg", None, "dup", "failed", "in use", "t2"),
    ]
    return run, actions


# ----- format selection -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "fmt", "expected"),
    [
        ("a.csv", None, "csv"),
        ("a.CSV", None, "csv"),
        ("a.xlsx", None, "xlsx"),
        ("a.html", None, "html"),
        ("a.htm", None, "html"),
        ("a.txt", None, "txt"),
        ("a.csv", "log", "log"),
        ("a.csv", "XLSX", "xlsx"),
        ("a.weird", ".html", "html"),
        ("noext", "txt", "txt"),
    ],
)
def test_format_for(name: str, fmt: str | None, expected: str) -> None:
    assert report.format_for(Path(name), fmt) == expected


@pytest.mark.parametrize(("name", "fmt"), [("a.docx", None), ("noext", None), ("a.csv", "pdf")])
def test_format_for_rejects_unknown(name: str, fmt: str | None) -> None:
    with pytest.raises(ValueError, match="format"):
        report.format_for(Path(name), fmt)


def test_export_filter_and_filter_lookup() -> None:
    assert set(report.FORMATS) == {"csv", "xlsx", "html", "txt", "log"}
    parts = report.EXPORT_FILTER.split(";;")
    assert parts[:4] == [
        "CSV (*.csv)",
        "Excel workbook (*.xlsx)",
        "HTML report (*.html)",
        "Review list (*.txt)",
    ]
    for fid, label in report.FORMATS.items():
        assert any(p.startswith(f"{label} (") for p in parts), fid
        assert report.format_from_filter(next(p for p in parts if p.startswith(label))) == fid
    assert report.format_from_filter("Everything (*)") is None


def test_export_rejects_unknown_format_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "out" / "r.docx"
    with pytest.raises(ValueError, match="format"):
        report.export_plan(simple_plan(tmp_path), target)
    assert not target.parent.exists()


# ----- sizes ----------------------------------------------------------------------------------


def test_action_sizes_is_metadata_only(tmp_path: Path) -> None:
    plan = simple_plan(tmp_path)
    plan.actions.append(PlannedAction(ActionKind.WRITE, None, tmp_path / "w.txt"))
    assert report.action_sizes(plan) == [5, None, None, None]


# ----- CSV ------------------------------------------------------------------------------------


def test_csv_plan_rows_and_bom(tmp_path: Path) -> None:
    plan = simple_plan(tmp_path)
    target = tmp_path / "nested" / "plan.csv"
    written = report.export_plan(plan, target, sizes=report.action_sizes(plan))
    assert written == target
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = read_csv(target)
    assert rows[0] == [
        "Selected",
        "Kind",
        "Source",
        "Destination",
        "Reason",
        "Group",
        "Size (bytes)",
    ]
    assert len(rows) == 4
    root = plan.roots[0]
    assert rows[1] == [
        "yes",
        "rename",
        str(root / "a.txt"),
        str(root / "b.txt"),
        "why <b>",
        "",
        "5",
    ]
    assert rows[2][0] == "no"
    assert rows[2][6] == ""
    assert not list(target.parent.glob("*.tmp"))


def test_csv_without_sizes_leaves_the_column_empty(tmp_path: Path) -> None:
    target = tmp_path / "plan.csv"
    report.export_plan(simple_plan(tmp_path), target)
    assert {row[6] for row in read_csv(target)[1:]} == {""}


def test_sizes_must_match_the_actions(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sizes"):
        report.export_plan(simple_plan(tmp_path), tmp_path / "p.csv", sizes=[1])


def test_csv_run(tmp_path: Path) -> None:
    run, actions = sample_run()
    target = tmp_path / "run.csv"
    report.export_run(run, actions, target)
    rows = read_csv(target)
    assert rows[0] == ["Seq", "Kind", "Status", "Source", "Destination", "Reason", "Error", "Time"]
    assert rows[1] == ["1", "move", "done", "C:/a/x.jpg", "C:/b/x.jpg", "dup", "", "t1"]
    assert rows[2] == ["2", "trash", "failed", "C:/a/y.jpg", "", "dup", "in use", "t2"]


def test_csv_result_joins_plan_and_outcomes(make_image: ImageMaker, tmp_path: Path) -> None:
    plan, keeper, dup_a, dup_b = duplicates_plan(make_image, tmp_path)
    result = RunResult(
        3,
        "partial",
        [
            ActionOutcome(1, "move", "done", str(dup_a), str(tmp_path / "duplicate" / "dup_a.jpg")),
            ActionOutcome(2, "trash", "failed", str(dup_b), None, "locked"),
        ],
    )
    target = tmp_path / "result.csv"
    report.export_result(plan, result, target)
    rows = read_csv(target)
    assert rows[0][-2:] == ["Group", "Detail"]
    assert rows[1][:3] == ["1", "move", "done"]
    assert rows[1][5].startswith("identical bytes")
    assert rows[1][6] == str(keeper)
    assert rows[2][2] == "failed"
    assert rows[2][7] == "locked"


def test_a_failed_export_leaves_no_temp_file_and_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "plan.csv"
    target.write_text("old", encoding="utf-8")

    def boom(_report: object, _target: Path) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(report, "_write_csv", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        report.export_plan(simple_plan(tmp_path), target)
    assert target.read_text(encoding="utf-8") == "old"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_csv_export_does_not_import_pillow(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from various_tools.core import report\n"
        "from various_tools.core.plan import ActionKind, Plan, PlannedAction\n"
        "plan = Plan('t', [Path('.')], [PlannedAction(ActionKind.TRASH, Path('x'))])\n"
        f"report.export_plan(plan, Path({str(tmp_path / 'a.csv')!r}))\n"
        "assert 'PIL' not in sys.modules, 'Pillow was imported'\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr


# ----- XLSX -----------------------------------------------------------------------------------


def test_xlsx_plan_has_both_sheets(tmp_path: Path) -> None:
    plan = simple_plan(tmp_path)
    target = tmp_path / "plan.xlsx"
    report.export_plan(plan, target, sizes=report.action_sizes(plan), tool_name="Rename files")
    book = load_workbook(target)
    assert book.sheetnames == ["Actions", "Summary"]
    actions = book["Actions"]
    assert [c.value for c in actions[1]][:3] == ["Selected", "Kind", "Source"]
    assert actions["A1"].font.bold
    assert actions.freeze_panes == "A2"
    assert actions.auto_filter.ref == "A1:G4"
    assert actions.max_row == 4
    assert actions["G2"].value == 5
    assert actions["C2"].value == str(plan.roots[0] / "a.txt")
    summary = {row[0].value: row[1].value for row in book["Summary"].iter_rows()}
    assert summary["Tool"] == "Rename files"
    assert summary["Tool id"] == "rename"
    assert summary["Created"] == "2026-01-02T03:04:05+00:00"
    assert summary["Actions"] == "3"
    assert summary["kind: trash"] == 2
    assert "match" in str(summary["Options"])
    assert any(
        row[0].value == "Note" and row[1].value == "a note & more" for row in book["Summary"]
    )


def test_xlsx_keeps_a_leading_equals_sign_as_text(tmp_path: Path) -> None:
    plan = Plan(
        "t",
        [tmp_path],
        [PlannedAction(ActionKind.TRASH, tmp_path / "x", reason="=SUM(1,1)")],
    )
    target = tmp_path / "p.xlsx"
    report.export_plan(plan, target)
    cell = load_workbook(target)["Actions"]["E2"]
    assert cell.value == "=SUM(1,1)"
    assert cell.data_type == "s"


def test_xlsx_run(tmp_path: Path) -> None:
    run, actions = sample_run()
    target = tmp_path / "run.xlsx"
    report.export_run(run, actions, target)
    book = load_workbook(target)
    sheet = book["Actions"]
    assert [c.value for c in sheet[1]][2] == "Status"
    assert sheet["A2"].value == 1
    assert sheet["G3"].value == "in use"
    summary = {row[0].value: row[1].value for row in book["Summary"].iter_rows()}
    assert summary["Run"] == "7"
    assert summary["status: done"] == 1
    assert summary["status: failed"] == 1


# ----- HTML -----------------------------------------------------------------------------------


def test_html_is_self_contained_and_escaped(tmp_path: Path) -> None:
    plan = simple_plan(tmp_path)
    plan.notes.append("<script>alert(1)</script>")
    target = tmp_path / "plan.html"
    report.export_plan(plan, target, tool_name="Rename & more")
    page = target.read_text(encoding="utf-8")
    assert page.startswith("<!DOCTYPE html>")
    assert "http" not in page
    assert "<script" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "why &lt;b&gt;" in page
    assert "Rename &amp; more" in page
    assert "a note &amp; more" in page
    assert html.escape(str(plan.roots[0] / "a.txt")) in page
    assert "<style>" in page


def test_html_duplicates_are_grouped_with_thumbnails(
    make_image: ImageMaker, tmp_path: Path
) -> None:
    plan, keeper, dup_a, dup_b = duplicates_plan(make_image, tmp_path)
    target = tmp_path / "dups.html"
    report.export_plan(plan, target, sizes=report.action_sizes(plan))
    page = target.read_text(encoding="utf-8")
    assert page.count('class="group"') == 1
    assert "Group 1: 2 duplicate(s)" in page
    assert page.count("data:image/jpeg;base64,") == 3  # the keeper and both duplicates
    assert "http" not in page
    for path in (keeper, dup_a, dup_b):
        assert path.name in page
    assert page.count("kept</span>") == 1


def test_html_without_thumbnails_has_no_images(make_image: ImageMaker, tmp_path: Path) -> None:
    plan, *_ = duplicates_plan(make_image, tmp_path)
    target = tmp_path / "dups.html"
    report.export_plan(plan, target, thumbnails=False)
    page = target.read_text(encoding="utf-8")
    assert "data:image" not in page
    assert "<img" not in page
    assert 'class="group"' in page


def test_html_placeholders_and_no_image_is_opened_for_cloud_only_files(
    make_image: ImageMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, keeper, dup_a, dup_b = duplicates_plan(make_image, tmp_path)
    cloud = {str(dup_a)}
    real_facts = thumbs.file_facts

    def facts(path: Path) -> thumbs.FileFacts:
        if str(path) in cloud:
            return thumbs.FileFacts(path, True, 10, datetime.now(), True)
        return real_facts(path)

    opened: list[Path] = []

    def spy_open(path: Path, *args: object, **kwargs: object) -> object:
        opened.append(path)
        raise OSError("unreadable on purpose")

    monkeypatch.setattr(thumbs, "file_facts", facts)
    monkeypatch.setattr(thumbs, "open_image", spy_open)
    target = tmp_path / "cloud.html"
    report.export_plan(plan, target)
    page = target.read_text(encoding="utf-8")
    assert "cloud-only: not downloaded" in page
    assert "no preview" in page  # the others cannot be decoded, which is also not an error
    assert "data:image" not in page
    assert dup_a not in opened, "a cloud-only file must never be opened"


def test_html_marks_a_missing_file(make_image: ImageMaker, tmp_path: Path) -> None:
    plan, keeper, dup_a, _ = duplicates_plan(make_image, tmp_path)
    dup_a.unlink()
    target = tmp_path / "gone.html"
    report.export_plan(plan, target)
    assert "missing" in target.read_text(encoding="utf-8")


def test_html_run_report(tmp_path: Path) -> None:
    run, actions = sample_run()
    target = tmp_path / "run.html"
    report.export_run(run, actions, target)
    page = target.read_text(encoding="utf-8")
    assert "run 7 (partial)" in page
    assert "in use" in page
    assert "<img" not in page


# ----- the old formats ------------------------------------------------------------------------


def test_txt_review_list_for_duplicates(make_image: ImageMaker, tmp_path: Path) -> None:
    plan, keeper, dup_a, dup_b = duplicates_plan(make_image, tmp_path)
    target = tmp_path / "review.txt"
    report.export_plan(plan, target)
    text = target.read_text(encoding="utf-8")
    assert "DRY RUN (nothing was changed)" in text
    assert "Folders scanned:" in text
    assert text.index("TO DUPLICATE") < text.index("TO REVIEW")
    assert f"[exact] {dup_a}" in text
    assert f"would move to: {tmp_path / 'duplicate' / dup_a.name}" in text
    assert f"[visual] {dup_b}" in text
    assert "would send to the Recycle Bin" in text
    assert f"keeps   : {keeper}" in text
    assert "why     : identical bytes (SHA-256); keeps keep.jpg" in text
    to_review = text[text.index("TO REVIEW") :]
    assert "(none)" in to_review


def test_txt_for_another_tool_groups_by_kind(tmp_path: Path) -> None:
    target = tmp_path / "plan.txt"
    report.export_plan(simple_plan(tmp_path), target)
    text = target.read_text(encoding="utf-8")
    assert "RENAME\n" in text
    assert "TRASH\n" in text
    assert "(not selected)" in text
    assert "would rename to:" in text


def test_txt_run_says_what_happened(tmp_path: Path) -> None:
    run, actions = sample_run()
    target = tmp_path / "run.txt"
    report.export_run(run, actions, target)
    text = target.read_text(encoding="utf-8")
    assert "APPLIED" in text
    assert "moved to: C:/b/x.jpg" in text
    assert "failed: in use" in text


def test_log_csv_matches_the_old_duplicate_log(make_image: ImageMaker, tmp_path: Path) -> None:
    plan, keeper, dup_a, dup_b = duplicates_plan(make_image, tmp_path)
    target = tmp_path / "log.csv"
    report.export_plan(plan, target, "log")
    rows = read_csv(target)
    assert rows[0] == [
        "moved_file",
        "moved_to",
        "original_name",
        "kept_original",
        "match_type",
        "original_path",
        "kept_path",
    ]
    assert rows[1] == [
        "dup_a.jpg",
        "duplicate",
        "dup_a.jpg",
        "keep.jpg",
        "exact",
        str(dup_a),
        str(keeper),
    ]
    assert rows[2][1] == "Recycle Bin"
    assert rows[2][4] == "visual"


def test_log_csv_of_a_run_lists_only_what_was_done(tmp_path: Path) -> None:
    run, actions = sample_run()
    target = tmp_path / "log.csv"
    report.export_run(run, actions, target, "log")
    rows = read_csv(target)
    assert len(rows) == 2
    assert rows[1][:3] == ["x.jpg", "b", "x.jpg"]
