.PHONY: default format lint install check venv coverage

default: check

venv:
	uv venv --clear
	uv sync

format: venv
	uv tool run black . --target-version py310
	prettier . --write
	uv run cfengine format ./

lint: venv
	uv tool run black --check . --fast
	uv tool run flake8 src/ --ignore=E203,W503,E722,E731 --max-complexity=100 --max-line-length=160
	uv tool run pyflakes src/
	uv tool run pyright src/
	uv run cfengine lint --strict=no ./
	uv run cfengine format --check

install:
	pipx install --force --editable .


coverage: export COVERAGE_PROCESS_START = $(PWD)/.coveragerc
coverage: export COVERAGE_FILE = $(PWD)/.coverage
coverage: export PROJECT_ROOT = $(PWD)
coverage:
	uv run coverage erase
	uv run coverage run --parallel-mode -m pytest
	uv run bash tests/run-lint-tests.sh
	uv run bash tests/run-format-tests.sh
	uv run bash tests/run-shell-tests.sh
	uv run bash tests/run-up-validate-tests.sh
	uv run coverage combine
	uv run coverage report --fail-under=50
	uv run coverage xml

check: venv format lint
	uv run pytest
	uv run bash tests/run-lint-tests.sh
	uv run bash tests/run-format-tests.sh
	uv run bash tests/run-shell-tests.sh
	uv run bash tests/run-up-validate-tests.sh
