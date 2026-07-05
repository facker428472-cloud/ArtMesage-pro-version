FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    Flask==2.3.3 \
    Flask-SocketIO==5.3.4 \
    Flask-CORS==4.0.0 \
    python-socketio==5.9.0 \
    eventlet==0.33.3

COPY server.py .

RUN mkdir -p /app/photos /app/data

EXPOSE 5000

ENV PYTHONUNBUFFERED=1
ENV DB_PATH=/app/data/artmessage.db
ENV PHOTOS_DIR=/app/photos

CMD ["python", "server.py"]
