FROM python:3.11-slim

# шрифты для карточек (Pillow) + tzdata для МСК
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Europe/Moscow \
    POST_SCHEDULE=1

# база и картинки живут в томах — не теряются при пересборке
VOLUME ["/app/data", "/app/images", "/app/logs"]

# бот отвечает на команды мгновенно и публикует по расписанию
CMD ["python", "run_bot.py"]
