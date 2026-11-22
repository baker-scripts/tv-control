# tv-control talks to an ADB server on 127.0.0.1:5037 that other containers may share.
# An adb client of another version kills and restarts that server, so keep ANDROID_TOOLS (and the
# Alpine release that ships it) equal to the adb version of any other container on the same server.
FROM mirror.gcr.io/library/python:3.15-alpine3.24@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
ARG ANDROID_TOOLS=35.0.2-r21
# ssh (the WoL relay) refuses a uid with no passwd entry, so the image names the runtime user.
# Run the container as TV_UID:TV_GID, or mount a passwd entry for another uid.
ARG TV_UID=1000
ARG TV_GID=1000
RUN apk add --no-cache "android-tools=${ANDROID_TOOLS}" bash yq-go openssh-client-default \
    && adb version | grep -qx "Version ${ANDROID_TOOLS%-r*}-android-tools" \
    && addgroup -g "${TV_GID}" tvcontrol \
    && adduser -D -H -h /config -s /sbin/nologin -u "${TV_UID}" -G tvcontrol tvcontrol
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN uv sync --locked --no-dev --no-install-project
COPY *.py ./
COPY static ./static
ARG TV_CONTROL_VERSION=unknown
ARG TV_CONTROL_GIT_SHA=unknown
ARG TV_CONTROL_GIT_DATE=unknown
ENV PYTHONUNBUFFERED=1 TV_CONTROL_VERSION=${TV_CONTROL_VERSION} TV_CONTROL_GIT_SHA=${TV_CONTROL_GIT_SHA} \
    TV_CONTROL_GIT_DATE=${TV_CONTROL_GIT_DATE}
# Footer build date. The ARGs above change per commit, so this layer reruns for each new build.
RUN date -u +%Y-%m-%dT%H:%M:%SZ > /app/BUILD_DATE
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8011/healthz', timeout=4)"]
CMD ["python3", "/app/app.py"]
