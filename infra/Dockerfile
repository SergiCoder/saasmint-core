FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV UV_FROZEN=1

COPY pyproject.toml uv.lock* /app/
COPY core /app/core
RUN uv sync --no-dev

COPY config /app/config
COPY apps /app/apps
COPY middleware /app/middleware
COPY manage.py helpers.py /app/
COPY templates /app/templates
COPY static /app/static
COPY infra/entrypoint.sh infra/uvicorn-log-config.json /app/infra/

RUN mkdir -p /app/media /app/staticfiles \
    && addgroup --system --gid 1000 app \
    && adduser --system --uid 1000 --ingroup app app \
    && chown -R app:app /opt/venv /tmp/uv-cache /app/media /app/staticfiles
USER app

ENV DJANGO_PORT=8001
EXPOSE ${DJANGO_PORT}

CMD ["sh", "-c", "uv run uvicorn config.asgi:application --host 0.0.0.0 --port ${DJANGO_PORT} --workers 4"]
