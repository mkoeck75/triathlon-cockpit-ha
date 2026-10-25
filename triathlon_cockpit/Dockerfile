FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app

# Läuft als einfacher Prozess (init: false); Supervisor-Watchdog prüft /api/health.
CMD ["python", "-m", "cockpit.main"]
