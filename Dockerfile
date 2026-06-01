# =============================================================================
# Document-to-Criteria Checker
# =============================================================================

FROM python:3.11-slim

# Install system dependencies needed by pdfplumber (pdfminer) and Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        poppler-utils \
        && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (separate layer for cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project source
COPY app/        ./app/
COPY eval/       ./eval/
COPY tests/      ./tests/
COPY config.json .
COPY run_eval.sh .
RUN chmod +x run_eval.sh

# Copy annotation corpus and demo schema (eval ground truth — not secrets)
COPY data/annotation/ ./data/annotation/
COPY data/demo_schema_singapore_citizenship.sql ./data/

# PDF and infographic data are mounted at runtime (see docker-compose.yml)
# so users can bring their own documents without rebuilding the image.
RUN mkdir -p data/pdf data/infographic eval/results

# Streamlit config — disable telemetry and set server defaults
RUN mkdir -p /root/.streamlit
RUN printf '[general]\nemail = ""\n' > /root/.streamlit/credentials.toml
RUN printf '[server]\nheadless = true\nport = 8501\nenableCORS = false\nenableXsrfProtection = false\n[browser]\ngatherUsageStats = false\n' \
    > /root/.streamlit/config.toml

# Expose Streamlit port
EXPOSE 8501

# Default: launch the Streamlit UI
# Override with:  docker run ... python3 -m pytest tests/
#                 docker run ... ./run_eval.sh exp1
CMD ["python3", "-m", "streamlit", "run", "app/ui/streamlit_app.py", "--server.address=0.0.0.0"]
