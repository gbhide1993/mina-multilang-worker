FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run the worker using direct Python import
CMD ["python", "-c", "from rq import Worker; from redis import from_url; import os; w=Worker(['default'], connection=from_url(os.environ['REDIS_URL'])); w.work(with_scheduler=True)"]
