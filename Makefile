DOCKER_NAME=hush-hosted-agent-tutorial
RELEASE=$(shell git describe --always --dirty --abbrev=10)
VENV=.venv
CHART_DIR=charts/hush-hosted-agent-tutorial
# Placeholders, only for rendering the chart here.
CHART_SAMPLE=gateway.url=https://gateway.example.com/mcp,gateway.issuer=https://hush.example.com/issuer

.PHONY: all
all: test lint build

.PHONY: venv
venv: $(VENV)/.installed

$(VENV)/.installed: requirements.txt requirements-dev.txt
	@python3 -m venv $(VENV)
	@$(VENV)/bin/pip install -q -r requirements-dev.txt
	@touch $@

.PHONY: test
test: venv
	@$(VENV)/bin/python -m pytest -q

# Checks the chart, both with a quick tunnel and with Expenses' own address.
.PHONY: lint
lint:
	@helm lint --quiet --strict $(CHART_DIR) --set $(CHART_SAMPLE)
	@helm lint --quiet --strict $(CHART_DIR) --set $(CHART_SAMPLE),expenses.url=https://expenses.example.com

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
