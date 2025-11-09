FROM python:3.11-slim

WORKDIR /app

COPY ../base-bot-template/ /app/base/
COPY . /app/strategy/

ENV PYTHONPATH="/app/strategy:/app/base"

RUN pip install --no-cache-dir -r /app/base/requirements.txt \
    && pip install --no-cache-dir -r /app/strategy/requirements.txt

EXPOSE 8080 3010

CMD ["python", "/app/strategy/startup.py"]
