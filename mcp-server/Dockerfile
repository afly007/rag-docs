FROM python:3.12-slim

WORKDIR /app

COPY mcp-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp-server/server.py .
COPY lib/ lib/

EXPOSE 8000

CMD ["python", "server.py"]
