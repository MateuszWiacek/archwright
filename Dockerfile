# python:3.12-slim pinned to its content digest. Floating tags can be
# retagged on Docker Hub; pinning to a digest fails the build instead
# of pulling something we did not approve. Bump via:
#   docker pull python:3.12-slim && docker inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
FROM python:3.12-slim@sha256:401f6e1a67dad31a1bd78e9ad22d0ee0a3b52154e6bd30e90be696bb6a3d7461

# pip install also pins to lockfile hashes; see requirements-web.lock.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        docker-cli \
        openssh-client \
        postgresql-client \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md requirements-web.lock ./
COPY backup ./backup
COPY archwright_web ./archwright_web

RUN pip install --no-cache-dir --require-hashes -r requirements-web.lock \
    && pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin archwright

USER archwright
WORKDIR /workspace

EXPOSE 8471

ENTRYPOINT ["archwright"]
CMD ["serve", "--config-dir", "/config", "--host", "0.0.0.0", "--port", "8471"]
