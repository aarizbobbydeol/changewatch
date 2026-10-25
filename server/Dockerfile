FROM python:3.12-slim

WORKDIR /app

# Install system dependencies if required
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install pinned requirements
COPY server/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy server code and sql migrations
COPY server /app/server
# The default price book, so `python -m app.cli price-book publish config/price_book.default.yaml` works
COPY config/price_book.default.yaml /app/server/config/price_book.default.yaml

ENV PYTHONPATH=/app/server
WORKDIR /app/server

EXPOSE 8000

# API worker processes from API_WORKERS (see app/serve.py)
CMD ["python", "-m", "app.serve"]
