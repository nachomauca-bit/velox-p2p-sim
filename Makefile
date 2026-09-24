# Velox P2P control-gate simulator — tasks.
# Works with GNU make on Linux/macOS and on Windows (Git Bash or cmd.exe).

ifeq ($(OS),Windows_NT)
  PY_BOOT ?= py -3
  VENV_PY := .venv/Scripts/python.exe
else
  PY_BOOT ?= python3
  VENV_PY := .venv/bin/python
endif
PORT ?= 8010

.PHONY: setup seed pdfs extract run test

setup:           ## create the virtualenv and install dependencies (Python 3.11+)
	$(PY_BOOT) -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 'Python 3.11+ required')"
	$(PY_BOOT) -m venv .venv
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r requirements.txt

seed:            ## generate the 12 PDFs, (re)create the DB, seed both scenarios, load the mailboxes
	$(VENV_PY) -m app.invoices_gen
	$(VENV_PY) -m app.seed

pdfs:            ## regenerate the 12 sample PDFs only
	$(VENV_PY) -m app.invoices_gen

extract:         ## Gemini extraction of the 12 PDFs (cached; FORCE=1 re-calls the API)
	$(VENV_PY) -m app.extract $(if $(filter 1 yes true,$(FORCE)),--force-extract,)

run:             ## serve the app on http://127.0.0.1:$(PORT)
	$(VENV_PY) -m uvicorn app.main:app --reload --reload-dir app --port $(PORT)

test:            ## unit tests (no API calls: extraction is mocked with fixtures)
	$(VENV_PY) -m pytest
