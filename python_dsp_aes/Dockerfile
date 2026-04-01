FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir flask gunicorn cryptography
COPY app.py .
EXPOSE 5000
# PYTHON_WORKERS: number of gunicorn workers (default 1 = true M/G/1)
# Set to N > 1 for M/G/N multi-worker experiment.
CMD ["sh", "-c", "gunicorn --workers ${PYTHON_WORKERS:-1} --bind 0.0.0.0:5000 --timeout 30 app:app"]
