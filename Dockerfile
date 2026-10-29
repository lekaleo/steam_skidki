FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow

WORKDIR /app

# системные зависимости (шрифты-фолбэк для Pillow)
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# непривилегированный пользователь (безопасность: не root)
RUN useradd -m appuser \
    && mkdir -p data images assets/fonts \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "run_bot.py"]
