FROM python:3.11-slim

WORKDIR /app

# System deps for lxml (often already OK; keep minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2 \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data

EXPOSE 8222

ENV PORT=8222

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8222"]
