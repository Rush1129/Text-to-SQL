# Dockerfile.render — FastAPI backend only (no React, no Streamlit)
FROM python:3.11-slim

# System deps for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY pipeline.py .
COPY audit.py .
COPY auth.py .
COPY db_models.py .
COPY rbac.py .
COPY schema.json .
COPY retrieval_documents.json .

COPY guardrails/ ./guardrails/
COPY models/ ./models/
COPY schema/ ./schema/
COPY verification/ ./verification/
COPY examples/ ./examples/
COPY prompts/ ./prompts/
COPY outputs/ ./outputs/

RUN mkdir -p outputs chroma_db

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
