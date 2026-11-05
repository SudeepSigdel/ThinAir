# Image used only to `git clone` a repository into the workspace volume. Built locally on first use.
# It must own /workspace as the sandbox user: Docker re-applies the ownership of an image's mount
# directory to an *empty* named volume whenever a container is created, so an image without
# /workspace (like the upstream one) would hand the volume back to root and break the clone.
# No package-manager network access is needed to build this image.
FROM alpine/git:v2.54.0
RUN adduser -D -u 10001 -H -s /sbin/nologin agent \
    && mkdir /workspace && chown 10001:10001 /workspace
WORKDIR /workspace
USER 10001:10001
