FROM python:3.11-slim

WORKDIR /app

# System deps for opencv-headless and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all source folders
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY pipeline/ ./pipeline/
COPY tools/ ./tools/
COPY config/ ./config/
COPY tests/ ./tests/

# Create data and output directories
RUN mkdir -p data/raw output/events

# Default DB URL
ENV DATABASE_URL=sqlite:///./data/store_intelligence.db

EXPOSE 8000

CMD ["sh", "-c", "python scripts/init_db.py && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
