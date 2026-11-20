# Thinair server image. No apt: nothing here needs system packages (cloning, builds and tests all
# happen in separate sandbox containers), which also keeps the build working on networks where
# Debian mirrors are slow or blocked.
# Pinned by digest (the multi-arch index, so x86 and ARM both resolve); Dependabot proposes updates.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# Optional package mirror for restricted networks: `THINAIR_PIP_INDEX_URL` in .env (see compose).
ARG PIP_INDEX_URL=""
# Optional extras, e.g. "postgres" for THINAIR_DATABASE_URL (`THINAIR_EXTRAS` in .env).
ARG EXTRAS=""
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin thinair \
    # /data must already exist and belong to the service user: an empty named volume takes the
    # ownership of the directory it is mounted on, so without this SQLite could not write.
    && mkdir /data && chown 10001:10001 /data
WORKDIR /app
# constraints.txt exists only in release builds (release.yml pins the versions CI tested); the
# glob lets a local build (`docker compose up --build`) go without it.
COPY pyproject.toml README.md constraints.tx[t] ./
COPY src ./src
RUN if [ -z "$PIP_INDEX_URL" ]; then unset PIP_INDEX_URL; fi \
    && if [ -f constraints.txt ]; then set -- -c constraints.txt; else set --; fi \
    && pip install --no-cache-dir "$@" ".${EXTRAS:+[$EXTRAS]}" \
    && rm -rf /app
WORKDIR /data
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=3s CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["thinair", "serve"]
