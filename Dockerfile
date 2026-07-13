FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn>=0.30"

COPY main.py ./
COPY tracker/ tracker/
COPY scripts/ scripts/

EXPOSE 18209 18808

CMD ["python", "main.py"]
