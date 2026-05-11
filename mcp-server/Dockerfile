FROM python:3.12-slim

WORKDIR /app

COPY mcp-server/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY mcp-server/server.py .
COPY lib/ lib/

EXPOSE 8000

CMD ["python", "server.py"]
