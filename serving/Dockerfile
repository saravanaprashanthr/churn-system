# Churn API image. Build with `make serving-image`, which first downloads the production model into
# build/model (the fallback served when the registry is unreachable) and tags the image with the git SHA.
FROM python:3.11.15-slim

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BUNDLED_MODEL_DIR=/app/model \
    MLFLOW_DISABLE_AGENT_HINT=1

WORKDIR /app
COPY requirements/serving.txt requirements/
RUN pip install -r requirements/serving.txt

COPY serving/ serving/
COPY build/model/ model/

RUN useradd --create-home --uid 1000 api
USER api
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"

CMD ["uvicorn", "serving.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--no-access-log"]

LABEL org.opencontainers.image.revision=${GIT_SHA}
