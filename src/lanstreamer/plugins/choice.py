"""Which plugins are turned on.

The installer asks which built-in inputs to turn on and writes the answer to
setup.ini in the program folder; the admin page can change it later (stored in
the settings table). A plugin that is off is not offered for new inputs.
Standby is always on: every output needs its fallback picture.
"""

from __future__ import annotations

import configparser
import json
import logging
from pathlib import Path

from lanstreamer.core.config import InputConfig
from lanstreamer.core.store import Store

ALWAYS_ON = {"standby"}
OFF_BY_DEFAULT = {"test-pattern"}  # without installer (repo checkout) or not asked
KEY = "disabled_plugins"
APPLIED = "setup_ini_applied"  # the setup.ini text last taken over

log = logging.getLogger("lanstreamer.plugins")


def disabled(store: Store) -> set[str]:
    raw = store.get_setting(KEY)
    return set(json.loads(raw)) if raw else set(OFF_BY_DEFAULT)


class InUse(Exception):
    def __init__(self, inputs: list[str]):
        super().__init__(", ".join(inputs))
        self.inputs = inputs


def set_enabled(store: Store, plugin_id: str, enabled: bool) -> None:
    """Turn a plugin on or off. Raises ValueError for Standby, InUse while inputs use it."""
    off = disabled(store)
    if enabled:
        off.discard(plugin_id)
    else:
        if plugin_id in ALWAYS_ON:
            raise ValueError(f"{plugin_id} is always on")
        users = [i.name for o in store.outputs() for i in o.inputs if i.plugin == plugin_id]
        if users:
            raise InUse(users)
        off.add(plugin_id)
    store.set_setting(KEY, json.dumps(sorted(off)))


def read_setup(path: Path | None) -> tuple[str, dict[str, bool]]:
    """setup.ini → (its text, plugin id → on). Missing file: ("", {})."""
    if path is None or not path.is_file():
        return "", {}
    text = path.read_text(encoding="utf-8", errors="replace")
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        log.warning("Ignoring %s: %s", path, exc)
        return "", {}
    if not parser.has_section("plugins"):
        return text, {}
    return text, {key: value.strip().lower() in ("on", "1", "yes", "true")
                  for key, value in parser.items("plugins")}  # fmt: skip


def on_start(store: Store, setup_file: Path | None) -> None:
    """Take over the installer's choice: completely on the first start, later only
    when it was installed again with other answers (plugins in use stay on)."""
    text, choices = read_setup(setup_file)
    if store.get_setting(KEY) is None:  # first start
        off = (OFF_BY_DEFAULT | {p for p, on in choices.items() if not on}) - {p for p, on in choices.items() if on}
        off -= ALWAYS_ON
        store.set_setting(KEY, json.dumps(sorted(off)))
        store.set_setting(APPLIED, text)
        if "submissions" not in off:  # ready to use: viewers can submit right away
            for out in store.outputs():
                if not any(i.plugin == "submissions" for i in out.inputs):
                    store.add_input(out.id, InputConfig("submissions", "Short-video submissions",
                                                        "submissions", {}, priority=50))  # fmt: skip
        log.info("Plugins turned off: %s", ", ".join(sorted(off)) or "none")
        return
    if not text or store.get_setting(APPLIED) == text:
        return
    for plugin_id, on in choices.items():  # installed again with other answers
        try:
            set_enabled(store, plugin_id, on)
        except (ValueError, InUse) as exc:
            log.info("Setup: %s stays on (%s)", plugin_id, exc)
    store.set_setting(APPLIED, text)
    log.info("Plugins turned off (from the installer): %s", ", ".join(sorted(disabled(store))) or "none")
