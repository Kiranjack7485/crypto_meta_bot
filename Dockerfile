FROM python:3.11-slim

WORKDIR /app

# Install build deps - removed gcc, pandas wheels exist for 3.11-slim now
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only Python files, NOT .env
COPY main.py .

ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Kolkata

CMD ["python", "main.py"]