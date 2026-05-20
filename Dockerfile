FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    WORKSPACE_DIR=/app

# Install system dependencies (curl for healthchecks, ca-certificates for subscription fetch)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose ports
# 8000: Web Dashboard Panel
# 1080: Smart SOCKS5 Entry
# 20000-20100: Dedicated multi-country port pool
EXPOSE 8000 1080 20000-20100

# Start ProxyHub
CMD ["python", "main.py"]
