FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata: transcript timestamps follow TZ from .env (the container itself runs in UTC).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY *.py ./

# Non-root, and /app/data is the only writable path (compose: read_only + a named volume).
# The session file lives there: full account access, so 0700 and never in the image.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/data && chown app:app /app/data && chmod 700 /app/data
USER app

ENV PORT=8023 DATA_DIR=/app/data
EXPOSE 8023

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c 'import os,urllib.request;urllib.request.urlopen("http://127.0.0.1:%s/health" % os.environ.get("PORT","8023"), timeout=4).read()' || exit 1

CMD ["python", "main.py"]
