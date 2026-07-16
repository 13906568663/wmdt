FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn>=0.30" "mcp>=1.9" "httpx>=0.27"

COPY main.py mcp_server.py ./
COPY tracker/ tracker/
COPY scripts/ scripts/

EXPOSE 18209 18808 18883 18210

CMD ["python", "main.py"]
