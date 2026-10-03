"""Agent harness for Kilo Code's CLI (``kilo``).

Kilo's CLI is a fork of OpenCode and reads the same kind of config, in
``~/.config/kilo/kilo.json`` or ``kilo.jsonc`` (under ``$XDG_CONFIG_HOME``
when that's set): ``baseURL`` on the provider in use routes it through a
proxy. Connecting sets that, and disconnecting puts the file back exactly as
it was, comments included.

  https://kilo.ai/docs/code-with-ai/platforms/cli
"""

import os
from pathlib import Path

from petsitter.agents.opencode import OpenCodeAgent


def _config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(xdg) if xdg else Path.home() / ".config") / "kilo"


class KiloAgent(OpenCodeAgent):
    id = "kilo"
    display_name = "Kilo Code"
    description = "Open-source AI coding agent, in the terminal (kilo)"
    icon = "/static/agents/kilo.png"   # bundled: gui/agents/
    trickset_filters = {"X-Title": "Kilo Code*", "Model": "*"}     # it sends X-Title: Kilo Code
    config_paths = ["~/.config/kilo/kilo.json", "~/.config/kilo/kilo.jsonc", "$XDG_CONFIG_HOME/kilo/kilo.json"]
    file_label = "kilo.json"
    checked = "@kilocode/cli 7.8.3 (`kilo run`), sandbox, 2026-10-03"
    default_provider = "kilo"       # Kilo's own gateway, its default

    def config_file(self) -> Path:
        d = _config_dir()
        jsonc = d / "kilo.jsonc"
        return jsonc if jsonc.exists() and not (d / "kilo.json").exists() else d / "kilo.json"
