# Retraining image for the CronJob (deploy/retrain/): python -m training.train, then the promotion gate,
# which runs the model behavior tests (tests/model), so they ship in the image too.
#   docker build -f training/Dockerfile --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t churn-trainer:<sha> .
FROM python:3.11.15-slim

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MLFLOW_DISABLE_AGENT_HINT=1 \
    HOME=/tmp

WORKDIR /app
COPY requirements/trainer.txt requirements/
RUN pip install -r requirements/trainer.txt

COPY pyproject.toml ./
COPY training/ training/
COPY serving/ serving/
COPY tests/__init__.py tests/conftest.py tests/seed.py tests/
COPY tests/model/ tests/model/

RUN useradd --create-home --uid 1000 trainer
USER trainer
LABEL org.opencontainers.image.revision=${GIT_SHA}
