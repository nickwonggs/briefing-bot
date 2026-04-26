FROM python:3.12-slim

# Install tzdata for Singapore timezone support
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data directory for persistent task state
RUN mkdir -p /data

ENV TZ=Asia/Singapore
ENV TASK_DATA_DIR=/data

CMD ["python", "scheduler_daemon.py"]
