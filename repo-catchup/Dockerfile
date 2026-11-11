FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY src ./src

ENV PYTHONPATH=/app/src

CMD ["python", "-m", "worker.entrypoints.worker"]
