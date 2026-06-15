from __future__ import annotations

import tomllib
from pathlib import Path


def test_project_scripts_cover_all_worker_entrypoints() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    assert pyproject["project"]["scripts"] == {
        "miki-sorter": "miki_sorter_bot.main:main",
        "miki-show-ids": "miki_sorter_bot.show_ids:main",
        "miki-doctor": "miki_sorter_bot.diagnostics:main",
    }
