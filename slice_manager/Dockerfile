FROM python:3.12-slim
WORKDIR /app
COPY app /app/app
RUN pip install --no-cache-dir \
    fastapi uvicorn httpx paramiko pydantic pydantic-settings python-jose[cryptography] \
    redis==5.0.4 \
    rq==1.16.2 \
    pymysql==1.1.1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9002"]