FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir websockets aiomqtt

COPY server.py .
COPY public/ public/

# Runtime data lives in /data (mounted as volume)
ENV DATA_DIR=/data

EXPOSE 8080 8443 8765

CMD ["python3", "server.py"]
