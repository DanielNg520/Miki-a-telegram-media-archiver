PYTHON ?= .venv/bin/python

.PHONY: verify test compile deps doctor run show-ids

verify: test compile deps doctor

test:
	$(PYTHON) -m pytest -q

compile:
	$(PYTHON) -m compileall -q miki_sorter_bot tests

deps:
	$(PYTHON) -m pip check

doctor:
	$(PYTHON) -m miki_sorter_bot.diagnostics

run:
	$(PYTHON) -m miki_sorter_bot.main

show-ids:
	$(PYTHON) -m miki_sorter_bot.show_ids
