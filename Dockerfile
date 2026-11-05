# Thinair server image. No apt: nothing here needs system packages (cloning, builds and tests all
# happen in separate sandbox containers), which also keeps the build working on networks where
# Debian mirrors are slow or blocked.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# Optional package mirror for restricted networks: `THINAIR_PIP_INDEX_URL` in .env (see compose).
ARG PIP_INDEX_URL=""
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin thinair \
    # /data must already exist and belong to the service user: an empty named volume takes the
    # ownership of the directory it is mounted on, so without this SQLite could not write.
    && mkdir /data && chown 10001:10001 /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN if [ -z "$PIP_INDEX_URL" ]; then unset PIP_INDEX_URL; fi \
    && pip install --no-cache-dir . \
    && rm -rf /app
WORKDIR /data
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=3s CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["thinair", "serve"]
