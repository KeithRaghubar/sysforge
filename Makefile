.PHONY: dev build install clean test lint

all: test

dev-deps:
	yay -S python-black python-pytest

dev:
	source .venv/bin/activate && uv pip install -e .

venv:
	uv venv
	source .venv/bin/activate

build:
	python -m build --wheel --no-isolation

install:
	makepkg -si

test:
	pytest

test-v:
	pytest -v

clean:
	rm -rf dist/ .venv/ __pycache__/ *.egg-info/ .pytest_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
