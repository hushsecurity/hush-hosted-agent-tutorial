DOCKER_NAME=hush-hosted-agent-tutorial
RELEASE=$(shell git describe --always --dirty --abbrev=10)
VENV=.venv

.PHONY: all
all: test build

.PHONY: venv
venv: $(VENV)/.installed

$(VENV)/.installed: requirements.txt requirements-dev.txt
	@python3 -m venv $(VENV)
	@$(VENV)/bin/pip install -q -r requirements-dev.txt
	@touch $@

.PHONY: test
test: venv
	@$(VENV)/bin/python -m pytest -q

.PHONY: build
build:
	@docker build -t $(DOCKER_NAME):$(RELEASE) .
	@docker tag $(DOCKER_NAME):$(RELEASE) $(DOCKER_NAME):latest

# Pins every package the image installs, from the direct ones in
# requirements.in. Run it after changing requirements.in, or to upgrade.
.PHONY: lock
lock:
	@uv pip compile -q --no-annotate --python-version 3.11 requirements.in -o requirements.txt

.PHONY: clean
clean:
	@rm -rf $(VENV) .pytest_cache
	@find . -name __pycache__ -not -path './$(VENV)/*' -exec rm -rf {} +
