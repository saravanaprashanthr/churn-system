# Image for every pipeline component. Build from the repo root:
#   docker build -f pipeline/Dockerfile --build-arg GIT_SHA=$(git rev-parse --short HEAD) \
#       -t churn-pipeline:$(git rev-parse --short HEAD) .
FROM python:3.11.15-slim

ARG GIT_SHA=unknown
ENV CODE_VERSION=${GIT_SHA} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements/base.txt requirements/pipeline.txt requirements/
RUN pip install -r requirements/pipeline.txt

COPY data_quality/ data_quality/
COPY pipeline/ pipeline/

RUN useradd --create-home --uid 1000 pipeline
USER pipeline

LABEL org.opencontainers.image.revision=${GIT_SHA}
