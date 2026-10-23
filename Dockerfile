# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# analyticsoviet API image
#
# Dependencies come straight from pyproject.toml, so there is a single source
# of truth. The "redis" extra is installed so CACHE_BACKEND=redis works;
# add "ytdlp" to EXTRAS if you want the YouTube yt-dlp fallback in the image.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    PORT=8000 \
    WEB_CONCURRENCY=1

WORKDIR /app

# Non-root runtime user.
RUN groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid app --home-dir /app --no-create-home app

COPY pyproject.toml ./

RUN --mount=type=cache,target=/root/.cache/pip \
    python - <<'PY' > /tmp/requirements.txt
import tomllib
from pathlib import Path

project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
extras = project.get("optional-dependencies", {})
print("\n".join([*project["dependencies"], *extras.get("redis", []), *extras.get("s3", [])]))
PY

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements.txt && rm -f /tmp/requirements.txt

COPY --chown=app:app src ./src

# Writable location for the SQLite history database.
RUN mkdir -p /app/data && chown -R app:app /app/data

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health', timeout=3)"

CMD ["sh", "-c", "exec uvicorn main:app --app-dir src --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY} --proxy-headers"]
