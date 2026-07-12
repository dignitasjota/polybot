FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config/ ./config/
COPY src/ ./src/

# Run as a non-root user (B9). Own /app/data and /app/logs so the panel DB and
# structured logs stay writable.
RUN mkdir -p /app/data /app/logs \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "src.main"]
