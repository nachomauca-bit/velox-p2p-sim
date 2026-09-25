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
DATASET ?= v1
IMAGE ?= velox-p2p-sim
DOCKER_PORT ?= 8080

.PHONY: setup seed pdfs extract validate run test intake export docker-build docker-run

setup:           ## create the virtualenv and install dependencies (Python 3.11+)
	$(PY_BOOT) -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 'Python 3.11+ required')"
	$(PY_BOOT) -m venv .venv
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r requirements.txt

seed: pdfs       ## generate the sample documents, (re)create the DB, seed both scenarios, load the mailboxes
	$(VENV_PY) -m app.seed

pdfs:            ## (re)generate the sample documents: the 14 case PDFs and test set v2 (deterministic)
	$(VENV_PY) -m app.invoices_gen --all

extract:         ## Gemini extraction of the sample documents (cached; DATASET=v2 for test set v2; FORCE=1 re-calls the API)
	$(VENV_PY) -m app.extract --dataset $(DATASET) $(if $(filter 1 yes true,$(FORCE)),--force-extract,)

run:             ## serve the app on http://127.0.0.1:$(PORT)
	$(VENV_PY) -m uvicorn app.main:app --reload --reload-dir app --port $(PORT)

test:            ## unit tests (no API calls: extraction is mocked with fixtures)
	$(VENV_PY) -m pytest

intake:          ## one pass over the IMAP mailboxes, posting new messages to the webhook (IMAP_* in .env)
	$(VENV_PY) -m app.intake_imap --once

export:          ## write the KPI export (NDJSON in data/export/); loads BigQuery too when BQ_EXPORT=1
	$(VENV_PY) -m app.export_bq

validate:        ## compare the cached Gemini extractions with the ground truth and the goldens (docs/LIVE_VALIDATION.md)
	$(VENV_PY) -m app.validate_live

docker-build:    ## build the container image (run make pdfs first if data/invoices_v2 is missing)
	docker build -t $(IMAGE) .

docker-run:      ## run the image on http://127.0.0.1:$(DOCKER_PORT) with the settings in .env
	docker run --rm -p $(DOCKER_PORT):8080 --env-file .env $(IMAGE)
