.PHONY: all format lint typecheck test live-smoke web

all: help

UV_CACHE_DIR ?= .uv-cache
export UV_CACHE_DIR
PYTEST_DISABLE_PLUGIN_AUTOLOAD ?= 1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD

PYTHON_FILES=src tests
TEST ?= .

test:
	uv run pytest $(TEST)

lint:
	uv run ruff check .
	uv run ruff format $(PYTHON_FILES) --diff
	uv run ruff check --select I $(PYTHON_FILES)
	uv run ty check src/purplemux_client

typecheck:
	uv run ty check src/purplemux_client

format:
	uv run ruff format $(PYTHON_FILES)
	uv run ruff check --fix $(PYTHON_FILES)

live-smoke:
	uv run python -m purplemux_client.live_smoke $(ARGS)

web:
	uv run python -m purplemux_client.web $(ARGS)

help:
	@echo 'format     - run code formatters'
	@echo 'lint       - run linters and type checking'
	@echo 'typecheck  - run static type checking'
	@echo 'test       - run tests'
	@echo 'live-smoke - run a live PurpleMux adapter lifecycle check (set ARGS)'
	@echo 'web        - run the trusted local Python Runner UI (set ARGS)'
