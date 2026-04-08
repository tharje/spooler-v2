FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir websockets

COPY server.py .
COPY public/ public/

# Runtime data lives in /data (mounted as volume)
ENV DATA_DIR=/data

EXPOSE 8080 8765

CMD ["python3", "server.py"]
