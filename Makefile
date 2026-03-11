.PHONY: dev build install clean test

dev-deps:
	yay -S python-black

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
	SYSFORGE_CONFIG_DIR=$(PWD)/tests/data python tests/test_wrapper.py

clean:
	rm -rf dist/ .venv/ __pycache__/ *.egg-info/
