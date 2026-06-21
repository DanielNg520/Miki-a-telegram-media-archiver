PYTHON ?= .venv/bin/python

.PHONY: verify test compile deps lint format-check typecheck security audit package doctor runtime-check run show-ids

verify: test compile deps lint format-check typecheck security audit package

test:
	$(PYTHON) -m pytest -q

compile:
	$(PYTHON) -m compileall -q miki_sorter_bot tests

deps:
	$(PYTHON) -m pip check

lint:
	$(PYTHON) -m ruff check miki_sorter_bot tests

format-check:
	$(PYTHON) -m ruff format --check miki_sorter_bot tests

typecheck:
	$(PYTHON) -m mypy --ignore-missing-imports miki_sorter_bot

security:
	$(PYTHON) -m bandit -q -lll -r miki_sorter_bot

audit:
	$(PYTHON) -m pip_audit --local --progress-spinner off

package:
	$(PYTHON) -m pip wheel --no-deps --wheel-dir /tmp/miki-wheel .

doctor:
	$(PYTHON) -m miki_sorter_bot.diagnostics

runtime-check: doctor

run:
	$(PYTHON) -m miki_sorter_bot.main

show-ids:
	$(PYTHON) -m miki_sorter_bot.show_ids
