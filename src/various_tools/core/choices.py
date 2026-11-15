"""Choices that are only known at run time, for editable dropdowns (plan §3.4, Ollama models).

A text option can name a ``choices_from`` source in its ``json_schema_extra``. A front end calls
``dynamic_choices(source, ctx)`` (in a worker thread, because it may use the network) and offers
the result as suggestions. The option itself stays free text, so a value that is not in the list
is still accepted.

``dynamic_choices`` never raises: a source that fails, and an unknown source, give ``[]``.
There is no UI code and no Qt import here.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable

from various_tools.core.context import RunContext
from various_tools.core.jsonutil import as_dict, as_list

log = logging.getLogger(__name__)

ChoiceSource = Callable[[RunContext], list[str]]


def ollama_model_names(url: str, timeout: float) -> list[str]:
    """The installed model names from ``GET <url>/api/tags``, sorted and without duplicates.

    Raises ``ValueError`` for an address that is not http(s), and ``OSError`` (which includes
    ``urllib.error.URLError`` and timeouts) or ``ValueError`` (bad JSON) when the server does not
    give a usable answer. A reply with no model list gives ``[]``.
    """
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"not a web address: {url}")
    endpoint = f"{url.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout) as response:  # noqa: S310 - checked
            data: object = json.load(response)
    except urllib.error.HTTPError as exc:
        exc.close()  # an error response holds a temp file open
        raise
    names: set[str] = set()
    for entry in as_list(as_dict(data).get("models")):
        name = as_dict(entry).get("name")
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return sorted(names, key=lambda n: (n.casefold(), n))


def _ollama_models(ctx: RunContext) -> list[str]:
    # The saved ``ollama_url`` is tried first, as the duplicate finder does for its model check.
    location = ctx.deps.resolve_ollama(ctx.settings.ollama_url)
    if not location.valid:
        return []
    return ollama_model_names(
        location.url or ctx.settings.ollama_url, ctx.settings.ollama_tags_timeout_s
    )


_SOURCES: dict[str, ChoiceSource] = {"ollama_models": _ollama_models}


def register_source(name: str, source: ChoiceSource) -> None:
    """Add (or replace) the source called ``name``. Tools and plugins can bring their own."""
    _SOURCES[name] = source


def dynamic_choices(source: str, ctx: RunContext) -> list[str]:
    """The current choices from the source called ``source``. ``[]`` on any failure."""
    function = _SOURCES.get(source)
    if function is None:
        return []
    try:
        return list(function(ctx))
    except Exception as exc:  # a dropdown must never break its form: show no suggestions
        log.debug("Choice source %s failed: %s", source, exc)
        return []
