FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_ROOT_USER_ACTION=ignore
WORKDIR /app

# ffmpeg + certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -r requirements-web.txt

# Copy project
COPY . .

# Expose port
EXPOSE 8000

# Start with a single worker to preserve in-memory job state per process
CMD ["sh", "-c", "uvicorn web.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]
