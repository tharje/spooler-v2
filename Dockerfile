FROM python:3.12-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir websockets aiomqtt bcrypt pywebpush

COPY server.py state.py auth.py persistence.py discovery.py spoolman.py \
     ws_handler.py http_handler.py ./
COPY printers/ printers/
COPY public/ public/

# Runtime data lives in /data (mounted as volume)
ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8080 8443 8765

CMD ["python3", "server.py"]
