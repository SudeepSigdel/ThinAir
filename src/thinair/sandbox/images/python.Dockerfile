# Sandbox base image for Python projects. Built locally on first use (see project/prepare.py).
# No package-manager network access is needed to build this image (no apt): it only adds the
# unprivileged sandbox user and the workspace directory on top of the official image.
FROM python:3.12-slim
# pip caches into /tmp (thrown away), except during the install step, which points it at the
# per-repo dependency cache volume on /cache (owned by 10001 here, so Docker gives an empty
# cache volume that owner). PIP_NO_CACHE_DIR must stay unset: pip treats ANY value as "off".
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_CACHE_DIR=/tmp/pip
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent \
    && mkdir /workspace /cache && chown 10001:10001 /workspace /cache
WORKDIR /workspace
USER 10001:10001
