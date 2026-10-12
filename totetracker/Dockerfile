FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

WORKDIR /app

# poppler-utils (pdftoppm) rasterizes labels at the printer's native resolution
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py labels.py run.py ./
COPY templates/ ./templates/

# Runs as root inside the add-on container so it can write to the add-on's /data volume
CMD ["python", "run.py"]
