# One image for the bot and the dashboard. See DEPLOY.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY blackout.csv.example ./

# Database and blackout file live on a volume so they survive upgrades.
ENV PAIRS_DB_PATH=/data/pairs_bot.db BLACKOUT_FILE=/data/blackout.csv
VOLUME /data

RUN useradd --create-home bot && mkdir -p /data && chown bot /data
USER bot

CMD ["python", "main.py"]
