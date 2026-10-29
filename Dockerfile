# tv-control talks to an ADB server on 127.0.0.1:5037 that other containers may share.
# An adb client of another version kills and restarts that server, so keep ANDROID_TOOLS equal
# to the adb version of any other container on the same server.
FROM mirror.gcr.io/library/python:3.13-alpine3.24@sha256:8bcfa47b04b853a17aaf909362b847b2112fd6ec4105ea553ef9a43bafbb5916
ARG ANDROID_TOOLS=35.0.2-r21
RUN apk add --no-cache "android-tools=${ANDROID_TOOLS}" bash yq-go openssh-client-default \
    && adb version | grep -qx "Version ${ANDROID_TOOLS%-r*}-android-tools"
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN uv sync --locked --no-dev --no-install-project
COPY *.py ./
COPY static ./static
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8011/healthz', timeout=4)"]
CMD ["python3", "/app/app.py"]
