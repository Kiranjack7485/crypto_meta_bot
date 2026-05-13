FROM python:3.11-slim

WORKDIR /app

# Install system deps for pandas/ta
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all bot files
COPY main.py .
COPY .env .

# Railway needs unbuffered logs to show in real-time
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Kolkata

# Run the main trading bot
CMD ["python", "main.py"]