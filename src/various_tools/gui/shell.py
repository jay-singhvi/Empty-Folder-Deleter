"""Windows Explorer right-click menu: "Open with Various Tools" on folders.

Per-user only (``HKEY_CURRENT_USER``, so no admin rights). The menu is registered twice: on the
folder itself and on the empty space inside an open folder::

    Software\\Classes\\Directory\\shell\\VariousTools
    Software\\Classes\\Directory\\Background\\shell\\VariousTools

Each has a cascading submenu with one entry per tool in :data:`SHELL_TOOLS`. Explorer replaces
``%V`` in the command with the folder that was clicked.

There is no Qt in this module. The registry sits behind the small :class:`RegistryBackend`
protocol, so tests and scratch checks use :class:`FakeRegistry` and never touch the real registry.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol

MENU_NAME = "VariousTools"
MENU_KEY = r"Software\Classes\Directory\shell\VariousTools"
BACKGROUND_KEY = r"Software\Classes\Directory\Background\shell\VariousTools"
MENU_KEYS: tuple[str, ...] = (MENU_KEY, BACKGROUND_KEY)
MENU_LABEL = "Open with Various Tools"

# (tool id, menu label). The ids must match the tool registry; the GUI opens that tool's page.
SHELL_TOOLS: tuple[tuple[str, str], ...] = (
    ("empty_folders", "Find empty folders"),
    ("duplicates", "Find duplicates"),
)

# Keys the real backend may touch. A bug can never write or delete outside this branch.
_ALLOWED_PREFIX = "software\\classes\\"


class RegistryBackend(Protocol):
    """The three registry operations this module needs. Paths are relative to HKEY_CURRENT_USER."""

    def set_value(self, key_path: str, name: str, value: str) -> None:
        """Create the key if needed and set a string value. ``name`` "" is the default value."""
        ...

    def delete_tree(self, key_path: str) -> None:
        """Delete the key and everything below it. No error if it does not exist."""
        ...

    def get_value(self, key_path: str, name: str) -> str | None:
        """The string value, or None when the key or value does not exist."""
        ...


class WinRegBackend:
    """The real registry, through ``winreg``, ``HKEY_CURRENT_USER`` only.

    Refuses any key outside ``Software\\Classes``. On other systems every call raises ``OSError``.
    """

    @staticmethod
    def _check(key_path: str) -> None:
        if sys.platform != "win32":
            raise OSError("The Explorer right-click menu is only available on Windows.")
        if not key_path.lower().startswith(_ALLOWED_PREFIX) or key_path.endswith("\\"):
            raise OSError(f"Refusing to touch the registry key {key_path!r}.")

    def set_value(self, key_path: str, name: str, value: str) -> None:
        self._check(key_path)
        if sys.platform == "win32":
            import winreg

            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def get_value(self, key_path: str, name: str) -> str | None:
        self._check(key_path)
        if sys.platform != "win32":
            return None
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                value, _kind = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return value if isinstance(value, str) else str(value)

    def delete_tree(self, key_path: str) -> None:
        self._check(key_path)
        if sys.platform == "win32":
            self._delete(key_path)

    def _delete(self, key_path: str) -> None:
        if sys.platform != "win32":
            return
        import winreg

        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE
            )
        except FileNotFoundError:
            return
        children: list[str] = []
        with key:
            while True:
                try:
                    children.append(winreg.EnumKey(key, len(children)))
                except OSError:
                    break
        for child in children:
            self._delete(f"{key_path}\\{child}")
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except FileNotFoundError:
            return


class FakeRegistry:
    """A dict-based stand-in for tests. Key paths are case-insensitive, like the real registry.

    ``writes`` records every ``set_value`` and ``delete_tree`` call, so a test can check that
    nothing was written.
    """

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}
        self.writes: list[tuple[str, ...]] = []

    @staticmethod
    def _norm(key_path: str) -> str:
        return key_path.strip("\\").lower()

    def set_value(self, key_path: str, name: str, value: str) -> None:
        self.writes.append(("set", key_path, name, value))
        parts = key_path.strip("\\").split("\\")
        for end in range(1, len(parts) + 1):
            self.keys.setdefault(self._norm("\\".join(parts[:end])), {})
        self.keys[self._norm(key_path)][name.lower()] = value

    def get_value(self, key_path: str, name: str) -> str | None:
        return self.keys.get(self._norm(key_path), {}).get(name.lower())

    def delete_tree(self, key_path: str) -> None:
        self.writes.append(("delete", key_path))
        root = self._norm(key_path)
        for key in [k for k in self.keys if k == root or k.startswith(root + "\\")]:
            del self.keys[key]

    def has_key(self, key_path: str) -> bool:
        return self._norm(key_path) in self.keys


def launch_command(tool_id: str) -> str:
    """The command Explorer runs for a tool. ``%V`` is the clicked folder.

    A frozen (installed) app runs itself. From source, the windowless ``pythonw.exe`` next to the
    current interpreter is preferred, so no console window flashes.
    """
    if getattr(sys, "frozen", False):
        program = sys.executable
        return f'"{program}" --tool {tool_id} --folder "%V"'
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    program = str(windowless if windowless.is_file() else interpreter)
    return f'"{program}" -m various_tools.gui --tool {tool_id} --folder "%V"'


def install(backend: RegistryBackend | None = None) -> None:
    """Add the right-click menu for the current Windows user. Safe to run again."""
    registry = backend or WinRegBackend()
    # Start clean so an entry from an older version never lingers.
    for menu_key in MENU_KEYS:
        registry.delete_tree(menu_key)
    for menu_key in MENU_KEYS:
        registry.set_value(menu_key, "MUIVerb", MENU_LABEL)
        registry.set_value(menu_key, "SubCommands", "")
        if getattr(sys, "frozen", False):
            registry.set_value(menu_key, "Icon", sys.executable)
        for tool_id, label in SHELL_TOOLS:
            tool_key = f"{menu_key}\\shell\\{tool_id}"
            registry.set_value(tool_key, "MUIVerb", label)
            registry.set_value(f"{tool_key}\\command", "", launch_command(tool_id))


def uninstall(backend: RegistryBackend | None = None) -> None:
    """Remove the right-click menu. Does nothing when it is not there."""
    registry = backend or WinRegBackend()
    for menu_key in MENU_KEYS:
        registry.delete_tree(menu_key)


def is_installed(backend: RegistryBackend | None = None) -> bool:
    """True when the menu is registered in both places."""
    registry = backend or WinRegBackend()
    return all(registry.get_value(menu_key, "MUIVerb") is not None for menu_key in MENU_KEYS)


def set_enabled(enabled: bool, backend: RegistryBackend | None = None) -> None:
    """Install or remove the menu. Raises ``OSError`` when the registry cannot be written."""
    if enabled:
        install(backend)
    else:
        uninstall(backend)
