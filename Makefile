PYTHON ?= python3
PIP ?= pip3

.PHONY: install install-dev format lint test serve-reporting serve-bdc serve-reconciliation serve-billing

install:
	$(PIP) install -e .

install-dev:
	$(PIP) install -e .[pdf,dev,cli]

format:
	black apps libs tests

lint:
	mypy apps libs

test:
	pytest

serve-reporting:
	FLASK_APP=apps.reporting.app FLASK_RUN_PORT=5010 flask run --host=127.0.0.1

serve-reconciliation:
	FLASK_APP=apps.reconciliation.app FLASK_RUN_PORT=5030 flask run --host=127.0.0.1

serve-billing:
	FLASK_APP=apps.billing.app FLASK_RUN_PORT=5040 flask run --host=127.0.0.1

serve-bdc:
	FLASK_APP=apps.bdc_converter.app FLASK_RUN_PORT=5000 flask run --host=127.0.0.1
