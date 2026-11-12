"""Export text for one file (plan §3.2): Python comments, notebook cells and unreadable files.

Python comments are removed with ``tokenize``, not regular expressions. Only real COMMENT tokens
are removed, so string literals stay intact, including ordinary triple-quoted strings. The
original script's regex deleted those too.

The rest follows the original script (``legacy/python-v3-Extract_Project_Code.py``) so the output
matches it for ordinary files and notebooks.
"""

from __future__ import annotations

import io
import json
import tokenize
from pathlib import Path

from various_tools.core.fs import long_path
from various_tools.core.jsonutil import as_dict, as_list

BINARY_NOTE = "[Binary file or unsupported encoding: {}]"
NOTEBOOK_ERROR = "[Error parsing notebook: {}]"


def strip_python_comments(source: str) -> str:
    """``source`` without its comments. Lines left blank by that are dropped.

    A trailing comment is cut at its ``#``, so the code before it keeps its text. Lines inside a
    multi-line string are never dropped, because they are part of the string's value.
    Raises ``SyntaxError`` or ``tokenize.TokenError`` when the source cannot be tokenized.
    """
    comment_at: dict[int, int] = {}
    inside_string: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comment_at[token.start[0]] = token.start[1]
        if token.end[0] > token.start[0]:
            inside_string.update(range(token.start[0], token.end[0] + 1))

    kept: list[str] = []
    for row, line in enumerate(source.splitlines(keepends=True), start=1):
        if row in comment_at:
            ending = line[len(line.rstrip("\r\n")) :]
            line = line[: comment_at[row]] + ending
        if row in inside_string or line.strip():
            kept.append(line)
    return "".join(kept)


def _strip_or_keep(source: str) -> str:
    """Strip comments, or keep the source as it is when it cannot be tokenized."""
    try:
        return strip_python_comments(source)
    except SyntaxError, tokenize.TokenError:
        return source


def _joined(value: object) -> str:
    """A notebook text field: a string, or a list of strings that are joined together."""
    if isinstance(value, list):
        return "".join(str(item) for item in as_list(value))
    if isinstance(value, str):
        return value
    return ""


def notebook_text(
    raw: str,
    name: str,
    *,
    strip_comments: bool,
    include_markdown: bool,
    include_outputs: bool,
) -> str:
    """The code cells of a Jupyter notebook, as plain text, in the original script's layout."""
    try:
        notebook = as_dict(json.loads(raw.replace("\0", "")))
    except json.JSONDecodeError:
        return NOTEBOOK_ERROR.format(name)

    parts: list[str] = []
    for cell_value in as_list(notebook.get("cells")):
        cell = as_dict(cell_value)
        kind = cell.get("cell_type")
        source = _joined(cell.get("source"))
        if kind == "markdown":
            if include_markdown:
                parts.append(f"# MARKDOWN CELL\n{source}\n\n")
        elif kind == "code":
            if strip_comments:
                source = _strip_or_keep(source)
            if source.strip():
                parts.append(source if source.endswith("\n") else source + "\n")
                parts.append("\n")
            outputs = as_list(cell.get("outputs")) if include_outputs else []
            if outputs:
                parts.append("# CELL OUTPUT\n")
                for output_value in outputs:
                    output = as_dict(output_value)
                    if "text" in output:
                        parts.append(f"# {_joined(output['text'])}\n")
                    elif "data" in output:
                        data = as_dict(output["data"])
                        if "text/plain" in data:
                            parts.append(f"# {_joined(data['text/plain'])}\n")
                parts.append("\n")
    return "".join(parts)


def file_text(
    path: Path,
    *,
    strip_comments: bool,
    include_markdown: bool,
    include_outputs: bool,
) -> str:
    """The export text for one file. Reads the file once and opens nothing else.

    The caller checks OneDrive status first (AGENTS.md rule 3). A file that is not valid UTF-8
    becomes a placeholder line, as it did in the original script.
    """
    try:
        with open(long_path(path), encoding="utf-8") as handle:
            raw = handle.read()
    except UnicodeDecodeError:
        return BINARY_NOTE.format(path)
    suffix = path.suffix.lower()
    if suffix == ".ipynb":
        text = notebook_text(
            raw,
            str(path),
            strip_comments=strip_comments,
            include_markdown=include_markdown,
            include_outputs=include_outputs,
        )
    elif suffix == ".py" and strip_comments:
        text = _strip_or_keep(raw)
    else:
        text = raw
    # Null bytes break some readers, so they are removed, as the original script did.
    return text.replace("\0", "")
