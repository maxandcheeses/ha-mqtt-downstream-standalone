FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir paho-mqtt websockets

COPY app/ /app/

CMD ["python3", "main.py"]