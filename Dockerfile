FROM python:3.12-slim

LABEL version="0.1"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

ENV CONFIG_PATH=/app/config.yaml
ENV DB_PATH=/app/data/reminders.db
ENV PYTHONPATH=/app/src

CMD ["python", "src/main.py"]
