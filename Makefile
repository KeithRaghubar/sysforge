.PHONY: dev build install clean

dev:
	source .venv/bin/activate && uv pip install -e .

venv:
	uv venv
	source .venv/bin/activate

build:
	python -m build --wheel --no-isolation

install:
	makepkg -si

clean:
	rm -rf dist/ .venv/ __pycache__/ *.egg-info/
