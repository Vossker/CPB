FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src
COPY alembic.ini /app/
COPY alembic /app/alembic
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "cpb.main:app", "--host", "0.0.0.0", "--port", "8000"]

