# Sandbox base image for Node projects. Built locally on first use (see project/prepare.py).
# No package-manager network access is needed to build this image (no apt).
FROM node:22-slim
ENV npm_config_cache=/tmp/npm npm_config_update_notifier=false npm_config_fund=false
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent \
    && mkdir /workspace && chown 10001:10001 /workspace
WORKDIR /workspace
USER 10001:10001
