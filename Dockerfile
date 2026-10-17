FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PATH="/app/.venv/bin:$PATH"

# SQLite lives at /data/plushboard.db inside the container (db.py resolves the
# data dir to the filesystem root) — mount a volume there.
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
