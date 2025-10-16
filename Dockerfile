FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# ffmpeg + certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching
COPY requirements.txt web/requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -r requirements-web.txt

# Copy project
COPY . .

# Expose port
EXPOSE 8000

# Start without ':'
CMD ["python", "run_server.py"]
