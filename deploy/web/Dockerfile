# Realtime-Venus web service, Harness and Codex. Built from the repository root by compose.yaml.
ARG PYTHON_IMAGE=python:3.11.18-slim-trixie@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG CODEX_VERSION=0.153.4
ARG TARGETARCH
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODEX_HOME=/data/codex

# Codex runs task commands inside a bubblewrap sandbox and expects git, curl and rg.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap ca-certificates curl git ripgrep \
    && rm -rf /var/lib/apt/lists/*
RUN python -c "import io, os, tarfile, urllib.request; \
arch = {'amd64': 'x86_64', 'arm64': 'aarch64'}[os.environ['TARGETARCH']]; \
name = f'codex-{arch}-unknown-linux-musl'; \
url = f'https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/{name}.tar.gz'; \
archive = tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen(url, timeout=300).read())); \
open('/usr/local/bin/codex', 'wb').write(archive.extractfile(name).read())" \
    && chmod 755 /usr/local/bin/codex \
    && codex --version

WORKDIR /app
COPY harness/requirements.txt harness/
COPY demos/requirements-web.txt demos/constraints-web.txt demos/
# Downloaded wheels stay in BuildKit's cache, so a rebuild downloads only packages it has not seen.
RUN --mount=type=cache,target=/root/.cache/pip pip install -r demos/requirements-web.txt
COPY harness harness
COPY demos demos

# Not root: the sandbox options below relax seccomp and /proc, which root could abuse.
RUN useradd --create-home --uid 1000 venus
USER venus

EXPOSE 8032
# CODEX_HOME lives on the /data mount, which starts empty; Codex refuses a missing one.
ENTRYPOINT ["sh", "-c", "mkdir -p \"$CODEX_HOME\" && exec python -m demos.server.cli \"$@\"", "web"]
