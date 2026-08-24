# ============================================================
# Stage 1: Builder — install Python dependencies
# ============================================================
FROM python:3.11-slim AS builder

# System deps required to compile psycopg2, cryptography, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only requirements first for better Docker layer caching
COPY requirements.txt .

# Install into a virtual-env so we can copy it cleanly to the runtime stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir supervisor


# ============================================================
# Stage 2: Runtime — lean final image
# ============================================================
FROM python:3.11-slim

# Runtime system deps (libpq for psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtualenv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy application source code
COPY main.py .
COPY local_main.py .
COPY pipeline.py .
COPY streamlit_app.py .
COPY audit.py .
COPY auth.py .
COPY db_models.py .
COPY rbac.py .
COPY migrate_to_postgres.py .
COPY schema.json .
COPY retrieval_documents.json .
COPY supervisord.conf .

# Copy package directories
COPY guardrails/ ./guardrails/
COPY models/ ./models/
COPY schema/ ./schema/
COPY verification/ ./verification/
COPY examples/ ./examples/
COPY prompts/ ./prompts/

# Create required directories
RUN mkdir -p outputs chroma_db

# Copy output files (schema for pipeline startup)
COPY outputs/ ./outputs/

# Expose ports: FastAPI (8000) and Streamlit (8501)
EXPOSE 8000 8501

# Health check for the FastAPI backend
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/docs', timeout=5)" || exit 1

# Start both services via supervisord
CMD ["supervisord", "-c", "supervisord.conf"]
