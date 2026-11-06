# MLflow tracking + registry server: Postgres backend store, artifacts proxied by the server
# (clients and Kubernetes pods fetch models over HTTP, so no shared filesystem is needed).
FROM python:3.11.15-slim

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1
COPY requirements/mlflow-server.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN useradd --create-home --uid 1000 mlflow && mkdir /mlartifacts && chown mlflow /mlartifacts
USER mlflow
EXPOSE 5000
