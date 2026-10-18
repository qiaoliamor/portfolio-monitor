FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY portfolio_monitor ./portfolio_monitor
RUN uv sync --locked --no-dev
RUN useradd --create-home app && mkdir -p /app/data && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["sh", "-c", "uv run --no-sync portfolio serve --host 0.0.0.0 --port ${PORT:-8000}"]
