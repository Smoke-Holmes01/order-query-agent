FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS base
WORKDIR /service
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

FROM base AS bootstrap
COPY app ./app
COPY scripts ./scripts
COPY sql ./sql
CMD ["sh", "-c", "python -m scripts.init_db && python -m scripts.seed_db"]

FROM base AS runtime
RUN useradd --create-home --uid 10001 api
COPY app ./app
USER api
EXPOSE 8002
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
