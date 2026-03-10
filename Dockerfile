FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (Docker layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app.py .
COPY templates/ templates/

EXPOSE 5001

# Single worker is required to preserve the in-memory quote cache.
# PORT is injected at runtime from docker-compose environment (default: 5001).
# Shell form is used so ${PORT} is expanded from the container environment.
CMD sh -c "gunicorn --bind 0.0.0.0:${PORT:-5001} --workers 1 --timeout 30 app:app"
