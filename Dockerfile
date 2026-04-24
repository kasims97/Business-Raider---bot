FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .
COPY bot ./bot
COPY README.md .
RUN pip install --no-cache-dir .

CMD ["python", "-m", "bot.main"]
