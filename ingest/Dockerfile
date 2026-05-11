FROM python:3.12-slim

WORKDIR /app

COPY ingest/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY ingest/*.py .
COPY lib/ lib/

ENTRYPOINT ["python", "ingest.py"]
