# Realtime-Venus model server (GPU). Built from the repository root by compose.yaml.
ARG PYTHON_IMAGE=python:3.11.18-slim-trixie@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    MAX_NUM_FRAMES=100000

WORKDIR /app
COPY demos/requirements-model.txt demos/constraints-model.txt demos/
# Downloaded wheels stay in BuildKit's cache, so a rebuild downloads only packages it has not seen.
RUN --mount=type=cache,target=/root/.cache/pip pip install -r demos/requirements-model.txt
COPY demos/__init__.py demos/variants.py demos/
COPY demos/model demos/model

EXPOSE 8031
ENTRYPOINT ["python", "-m", "demos.model.server"]
