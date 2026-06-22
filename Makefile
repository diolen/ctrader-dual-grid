# Makefile — тесты

PYTHON ?= python

.PHONY: install test test-all test-integration test-coverage

install:
	$(PYTHON) -m pip install -r requirements-test.txt

test:
	$(PYTHON) -m pytest tests/ -v -m "not integration"

test-all:
	$(PYTHON) -m pytest tests/ -v -s

test-integration:
	$(PYTHON) -m pytest tests/test_integration_demo.py -v -s -m integration

test-coverage:
	$(PYTHON) -m pytest tests/ -m "not integration" \
		--cov=app/core --cov=app/presentation --cov=app/strategy --cov-report=term-missing
