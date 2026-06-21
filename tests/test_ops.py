from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from miki_sorter_bot.diagnostics import DiagnosticCheck, DiagnosticReport
from miki_sorter_bot.ops import _build_parser, _plist_xml, render, rotate_logs


def test_ops_parser_exposes_terminal_commands() -> None:
    parser = _build_parser()

    assert parser.parse_args(["health"]).command == "health"
    assert parser.parse_args(["watch", "--interval", "1.5"]).interval == 1.5
    assert parser.parse_args(["logrotate", "--max-mb", "2", "--keep", "3"]).keep == 3


@pytest.mark.parametrize(
    "arguments",
    (
        ["watch", "--interval", "0"],
        ["logrotate", "--max-mb", "-1"],
        ["logrotate", "--keep", "-1"],
    ),
)
def test_ops_parser_rejects_unsafe_numeric_values(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(arguments)


def test_ops_render_includes_status_and_diagnostics() -> None:
    report = DiagnosticReport(
        (
            DiagnosticCheck("ok", "database", "fine"),
            DiagnosticCheck("warning", "jobs", "failed=1"),
        )
    )
    status = {
        "database": "ok",
        "foreign_keys": True,
        "posts": 12,
        "unavailable_posts": 1,
        "unresolved_dead_letters": 2,
        "jobs": {"failed": 1, "pending": 3},
        "deliveries": {"sent": 10},
        "metrics": {"sort_deliveries": 10},
    }

    output = render(report, status)

    assert "MIKI SORTER health" in output
    assert "check: 1 warning" in output
    assert "failed=1" in output
    assert "sort_deliveries=10" in output


def test_ops_plist_binds_miki_sorter_program_and_workdir(tmp_path) -> None:
    xml = _plist_xml("com.duy.miki-sorter", "/tmp/bin/miki-sorter", tmp_path)

    assert "<string>com.duy.miki-sorter</string>" in xml
    assert "<string>/tmp/bin/miki-sorter</string>" in xml
    assert f"<string>{tmp_path}</string>" in xml
    assert "miki-sorter.out.log" in xml


def test_ops_plist_escapes_paths_for_valid_xml(tmp_path) -> None:
    xml = _plist_xml("com.example.miki&sorter", "/tmp/a&b/miki-sorter", tmp_path / "a&b")

    assert "com.example.miki&amp;sorter" in xml
    assert "/tmp/a&amp;b/miki-sorter" in xml
    assert "a&amp;b" in xml


def test_ops_logrotate_copytruncates_and_prunes(tmp_path) -> None:
    live = tmp_path / "miki-sorter.out.log"
    live.write_text("x" * 128, encoding="utf-8")

    actions = rotate_logs(tmp_path, max_bytes=16, keep=1)

    assert any(action.startswith("rotated miki-sorter.out.log") for action in actions)
    assert live.read_text(encoding="utf-8") == ""
    generations = sorted(tmp_path.glob("miki-sorter.out.log.*.gz"))
    assert len(generations) == 1
    with gzip.open(generations[0], "rt", encoding="utf-8") as handle:
        assert handle.read() == "x" * 128

    live.write_text("y" * 128, encoding="utf-8")
    actions = rotate_logs(tmp_path, max_bytes=16, keep=1)

    assert any(action.startswith("pruned miki-sorter.out.log") for action in actions)
    assert len(sorted(Path(tmp_path).glob("miki-sorter.out.log.*.gz"))) == 1
