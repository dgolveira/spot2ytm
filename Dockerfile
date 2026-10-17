FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SPOT2YTM_HOST=0.0.0.0 \
    SPOT2YTM_PORT=8000 \
    SPOT2YTM_DATA_DIR=/data \
    SPOT2YTM_IN_DOCKER=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY spot2ytm ./spot2ytm
RUN pip install . \
    && useradd --create-home --uid 1000 app \
    && mkdir /data && chown app:app /data

USER app
VOLUME ["/data"]
EXPOSE 8000
CMD ["spot2ytm", "--no-browser"]
