FROM python:3.12-slim

WORKDIR /app

# Dependencias del sistema necesarias para ortools
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY app /app/app

# ortools incluye el solver CP-SAT
# httpx para consultas async a Prometheus
# pymysql para conexión a MariaDB
RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn==0.29.0 \
    pydantic==2.7.1 \
    ortools==9.10.4067 \
    httpx==0.27.0 \
    pymysql==1.1.1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9003"]