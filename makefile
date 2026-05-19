SONAR_URL   ?= http://sonarqube.sonarqube.svc.cluster.local:9000
SONAR_TOKEN ?= $(shell cat /config/.sonarqube-token 2>/dev/null)
SCANNER     ?= /config/.local/sonar-scanner/bin/sonar-scanner
JAVA_HOME   ?= $(shell dirname $$(dirname $$(readlink -f $$(which java))))
export JAVA_HOME

TAG     := $(shell git rev-parse --short HEAD)
IMAGE   := contribute.void42.internal/golden/edgar-data-fetcher
PROJECT := edgar-data-fetcher
SRC     := setup.py
TESTS   := tests

all: build release deploy

# ── Quality ──────────────────────────────────────────────────
test:
	python3 -m pytest $(TESTS) \
		--cov=$(SRC) \
		--cov-report=xml:coverage.xml \
		--junitxml=test-results.xml \
		-q

analyze: test
	$(SCANNER) \
		-Dsonar.projectKey=$(PROJECT) \
		-Dsonar.sources=$(SRC) \
		-Dsonar.tests=$(TESTS) \
		-Dsonar.language=py \
		-Dsonar.python.coverage.reportPaths=coverage.xml \
		-Dsonar.python.xunit.reportPath=test-results.xml \
		-Dsonar.host.url=$(SONAR_URL) \
		-Dsonar.token=$(SONAR_TOKEN) \
		-Dsonar.scm.provider=git
	@echo "Dashboard: $(SONAR_URL)/dashboard?id=$(PROJECT)"

# ── Deploy ───────────────────────────────────────────────────
build:
	docker build -t $(IMAGE):$(TAG) .

release:
	docker push $(IMAGE):$(TAG)

.PHONY: all test analyze build release deploy security

# ── Security & SBOM ─────────────────────────────────────────
audit:
	pip-audit -r requirements.txt --desc 2>&1 || true
	@echo ""
	@echo "=== Renovate Dependency Report ==="
	LOG_LEVEL=warn npx renovate --platform=local --dry-run 2>&1 | grep -E "dependency|update|→|->|current|new" | head -30 || true

sbom:
	cyclonedx-py requirements requirements.txt --of json -o sbom.json
	curl -s -X POST "$(DTRACK_URL)/api/v1/bom" \
		-H "X-Api-Key: $(DTRACK_KEY)" \
		-H "Content-Type: multipart/form-data" \
		-F "autoCreate=true" \
		-F "projectName=$(PROJECT)" \
		-F "projectVersion=main" \
		-F "bom=@sbom.json" > /dev/null
	@echo "SBOM uploaded to Dependency-Track"

.PHONY: audit sbom security

security:
	pip-audit -r $(firstword $(wildcard requirements.txt Requirements.txt)) --desc
