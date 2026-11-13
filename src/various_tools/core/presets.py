"""Saved option presets: a named set of tool options that can be reloaded later.

The CLI uses them as ``--preset NAME``, the GUI as a dropdown. They live in one JSON file at
``Settings.presets_path()``::

    {"format": 1, "presets": {"<tool id>": {"<preset name>": {<options>}}}}

A preset stores only the options that differ from the defaults (``exclude_defaults``), so a later
release can change a default without every saved preset pinning the old value.

The file is written through a temporary file and ``os.replace``. A file that cannot be read is
logged and treated as empty; a read never changes it. Before a save or delete would replace such
a file, it is copied aside to ``presets.json.corrupt`` (numbered if that exists) so nothing the
user wrote is lost.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from various_tools.core.jsonutil import as_dict
from various_tools.core.settings import Settings

log = logging.getLogger(__name__)

PRESETS_FORMAT = 1


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("a preset needs a name")
    return cleaned


def _is_dict(value: object) -> bool:
    return isinstance(value, dict)


class PresetStore:
    """Read and write the presets file. Cheap to create; nothing is opened until it is used."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def for_settings(cls, settings: Settings) -> PresetStore:
        return cls(settings.presets_path())

    @property
    def path(self) -> Path:
        return self._path

    # ----- reading -------------------------------------------------------------------

    def _read(self) -> tuple[dict[str, dict[str, dict[str, Any]]], bool]:
        """The stored presets, and whether the file was unreadable (so a write must back it up)."""
        if not self._path.is_file():
            return {}, False
        try:
            data: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable presets file %s: %s", self._path, exc)
            return {}, True
        top = as_dict(data)
        if top.get("format") != PRESETS_FORMAT or not _is_dict(top.get("presets")):
            log.warning("Ignoring presets file %s: unsupported or malformed content", self._path)
            return {}, True
        presets: dict[str, dict[str, dict[str, Any]]] = {}
        for tool_id, by_name in as_dict(top["presets"]).items():
            if not _is_dict(by_name):
                log.warning("Ignoring malformed presets for tool %r in %s", tool_id, self._path)
                continue
            clean: dict[str, dict[str, Any]] = {}
            for name, options in as_dict(by_name).items():
                if _is_dict(options):
                    clean[str(name)] = as_dict(options)
                else:
                    log.warning("Ignoring malformed preset %r/%r in %s", tool_id, name, self._path)
            presets[str(tool_id)] = clean
        return presets, False

    def names(self, tool_id: str) -> list[str]:
        """Preset names for a tool, sorted ignoring case."""
        presets, _ = self._read()
        return sorted(presets.get(tool_id, {}), key=lambda n: (n.casefold(), n))

    def all(self) -> dict[str, list[str]]:
        """Every tool that has presets, with its preset names. Sorted by tool id, then name."""
        presets, _ = self._read()
        return {
            tool_id: sorted(by_name, key=lambda n: (n.casefold(), n))
            for tool_id, by_name in sorted(presets.items())
            if by_name
        }

    def get(self, tool_id: str, name: str) -> dict[str, Any]:
        """A copy of the stored options. Raises ``KeyError`` when the preset does not exist."""
        presets, _ = self._read()
        try:
            return json.loads(json.dumps(presets[tool_id][_clean_name(name)]))
        except KeyError:
            raise KeyError(f"no preset {name.strip()!r} for tool {tool_id!r}") from None

    # ----- writing -------------------------------------------------------------------

    def save(self, tool_id: str, name: str, options: BaseModel | Mapping[str, Any]) -> None:
        """Store ``options`` under ``name``, replacing a preset of the same name.

        A model is stored with only its non-default values. A mapping is stored as given and must
        be JSON-serialisable (values such as ``Path`` are written as text).
        """
        cleaned = _clean_name(name)
        if isinstance(options, BaseModel):
            values: dict[str, Any] = options.model_dump(mode="json", exclude_defaults=True)
        else:
            values = cast("dict[str, Any]", json.loads(json.dumps(dict(options), default=str)))
        presets, corrupt = self._read()
        presets.setdefault(tool_id, {})[cleaned] = values
        self._write(presets, corrupt)

    def delete(self, tool_id: str, name: str) -> bool:
        """Remove a preset. Returns False when there was none."""
        cleaned = _clean_name(name)
        presets, corrupt = self._read()
        if cleaned not in presets.get(tool_id, {}):
            return False
        del presets[tool_id][cleaned]
        if not presets[tool_id]:
            del presets[tool_id]
        self._write(presets, corrupt)
        return True

    def _write(self, presets: dict[str, dict[str, dict[str, Any]]], corrupt: bool) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if corrupt:
            self._back_up_corrupt()
        text = json.dumps({"format": PRESETS_FORMAT, "presets": presets}, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")
            os.replace(tmp_path, self._path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def _back_up_corrupt(self) -> None:
        target = self._path.with_name(self._path.name + ".corrupt")
        counter = 1
        while target.exists():
            counter += 1
            target = self._path.with_name(f"{self._path.name}.corrupt{counter}")
        shutil.copy2(self._path, target)
        log.warning("Kept the unreadable presets file as %s", target)

    # ----- using ---------------------------------------------------------------------

    def build_options[M: BaseModel](
        self,
        options_cls: type[M],
        tool_id: str,
        name: str | None,
        overrides: Mapping[str, Any] | None = None,
    ) -> M:
        """Options from a preset (when ``name`` is given), then explicit ``overrides``, validated.

        Raises ``KeyError`` for an unknown preset and ``pydantic.ValidationError`` for values the
        model rejects. Only the keys in ``overrides`` replace preset values.
        """
        values: dict[str, Any] = self.get(tool_id, name) if name is not None else {}
        values.update(overrides or {})
        return options_cls.model_validate(values)
