FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

# Dependencies come from uv.lock (--frozen), so every build installs the versions CI tested.
ENV PYTHONUNBUFFERED=1 TRACKING_APP_ROOT=/app \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --no-editable --no-cache --python /usr/local/bin/python3 \
    && useradd --system --uid 10001 --no-create-home cell
# Image hardening: Debian security fixes newer than the base tag, and no pip in the final image.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/python3 -m pip uninstall -y --disable-pip-version-check pip
USER cell
# Secrets (database URLs, gateway key) arrive as environment variables at runtime only.
CMD ["tracking-api"]
