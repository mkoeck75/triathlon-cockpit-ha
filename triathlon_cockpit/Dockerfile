FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# tzdata: der Supervisor setzt TZ passend zu HA – ohne Zeitzonendaten liefe die App in UTC.
RUN apk add --no-cache tzdata

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app

# Läuft als einfacher Prozess (init: false); Supervisor-Watchdog prüft /api/health.
CMD ["python", "-m", "cockpit.main"]
