# Churn system task runner. `make` (or `make help`) lists the targets.
# Settings come from the environment or from .env (copy .env.example). Nothing secret is written here.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

-include .env
export

VENV       := .venv/bin
PYTHON     := $(VENV)/python
COMPOSE    := docker compose
PSQL       := docker exec -i ds_interview_postgres psql -U ds_candidate -d ecommerce -v ON_ERROR_STOP=1
SHA        := $(shell git rev-parse --short HEAD)
PIPELINE_IMAGE ?= churn-pipeline:$(SHA)

AS_OF          ?= 2026-07-01
CUTOFF         ?= 2026-03-30
BACKFILL_START ?= 2025-01-06
BACKFILL_END   ?= 2026-03-30

.PHONY: help install lint format test test-unit pre-commit up down db-load sql-optimize dq \
        pipeline-image pipeline-compile pipeline-run backfill train run evidence-e

help: ## list targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*## ' Makefile | awk -F':.*## ' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

require-%:
	@if [ -z "$${$*:-}" ]; then echo "$* is not set: copy .env.example to .env or export it" >&2; exit 2; fi

# ---------------------------------------------------------------- development
install: ## create .venv (Python 3.11) with the pinned dev lock and install the git hooks
	uv venv --python 3.11 .venv
	uv pip sync --python $(PYTHON) requirements/dev.txt
	$(VENV)/pre-commit install --hook-type pre-commit --hook-type pre-push

lint: ## ruff (lint + format check) and mypy
	$(VENV)/ruff check .
	$(VENV)/ruff format --check .
	$(VENV)/mypy

format: ## apply ruff fixes and formatting
	$(VENV)/ruff check --fix .
	$(VENV)/ruff format .

test: require-DQ_TEST_DATABASE_URL ## all tests incl. Postgres integration tests; fails under 80% coverage
	$(VENV)/pytest --cov

test-unit: ## tests that need no database (no coverage gate)
	$(VENV)/pytest -m "not integration and not model" --no-cov

pre-commit: ## run every pre-commit hook on all files
	$(VENV)/pre-commit run --all-files

# ---------------------------------------------------------------- services and data
up: ## start Postgres, Adminer and the MLflow server
	$(COMPOSE) up -d --build
	@until $(COMPOSE) exec -T postgres pg_isready -U ds_candidate -d ecommerce >/dev/null 2>&1; do sleep 1; done
	@until curl -sf localhost:5001/health >/dev/null; do sleep 2; done
	@echo "Postgres :5432 · Adminer http://localhost:8080 · MLflow http://localhost:5001"

down: ## stop the services (data volumes are kept)
	$(COMPOSE) down

db-load: ## fresh database only: schema, the five CSVs (SETUP.md), DQ and dataset tables, MLflow database
	$(PSQL) -f /sql/schema.sql
	for t in customers products orders order_items events; do \
	  $(PSQL) -c "COPY $$t FROM '/data/$$t.csv' WITH (FORMAT csv, HEADER true, NULL '')"; done
	$(PSQL) -f /sql/01_dq_excluded_customers.sql -f /sql/02_dq_tables.sql -f /sql/03_ml_dataset_tables.sql
	$(PSQL) -f /sql/04_mlflow_database.sql
	$(PSQL) -c "VACUUM ANALYZE"

sql-optimize: ## Part A: indexes, summary tables, planner settings
	$(PSQL) -f /sql/20_indexes.sql -f /sql/21_summary_tables.sql -f /sql/22_planner_settings.sql

# ---------------------------------------------------------------- pipeline and model
dq: require-DATABASE_URL ## Part B: data quality gate as of AS_OF (exit 1 = blocked)
	$(PYTHON) -m data_quality --as-of $(AS_OF)

pipeline-image: ## Part C: build the component image tagged with the git SHA
	docker build -f pipeline/Dockerfile --build-arg GIT_SHA=$(SHA) -t $(PIPELINE_IMAGE) .

pipeline-compile: ## Part C: compile pipeline/pipeline.yaml for PIPELINE_IMAGE
	PIPELINE_IMAGE=$(PIPELINE_IMAGE) $(PYTHON) -m pipeline.pipeline --output pipeline/pipeline.yaml

pipeline-run: require-PGPASSWORD pipeline-image ## Part C: one pipeline run for CUTOFF (KFP DockerRunner)
	PIPELINE_IMAGE=$(PIPELINE_IMAGE) $(PYTHON) -m pipeline.run_local --cutoff $(CUTOFF) --snapshot-date $(AS_OF)

backfill: require-PGPASSWORD pipeline-image ## Part C: weekly runs BACKFILL_START..BACKFILL_END (~40 s each)
	PIPELINE_IMAGE=$(PIPELINE_IMAGE) $(PYTHON) -m pipeline.run_local --snapshot-date $(AS_OF) \
	  --backfill-start $(BACKFILL_START) --backfill-end $(BACKFILL_END)

train: require-DATABASE_URL ## Part D: retrain on all published datasets, register alias 'candidate'
	$(PYTHON) -m training.train

run: up dq pipeline-run train ## end to end: services -> DQ gate -> pipeline (CUTOFF) -> training (needs `make backfill` once)

# ---------------------------------------------------------------- evidence
evidence-e: require-DQ_TEST_DATABASE_URL ## Part E: refresh evidence/tests and evidence/lint
	mkdir -p evidence/tests evidence/lint
	$(VENV)/pytest -v --cov --cov-report=term-missing > evidence/tests/pytest_output.txt 2>&1
	$(VENV)/ruff check . > evidence/lint/ruff.txt 2>&1
	$(VENV)/ruff format --check . > evidence/lint/ruff_format.txt 2>&1
	$(VENV)/mypy > evidence/lint/mypy.txt 2>&1
	$(VENV)/pre-commit run --all-files > evidence/lint/pre_commit.txt 2>&1
