FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY puente ./puente
COPY mock_saas ./mock_saas
COPY mock_zoho ./mock_zoho
COPY demo ./demo
RUN pip install --no-cache-dir ".[mock]"

EXPOSE 8000
CMD ["uvicorn", "mock_saas.app:app", "--host", "0.0.0.0", "--port", "8000"]
