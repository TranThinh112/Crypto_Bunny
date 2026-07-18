FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY . /app
CMD ["python", "-m", "crypto_trader", "ui", "--config", "config.railway.yaml", "--host", "0.0.0.0", "--port", "8000"]
