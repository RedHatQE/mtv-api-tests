# syntax=docker/dockerfile:1

# ------------------------------------------------------------------------------
# Stage 1: builder - install build deps, compile extensions, sync dependencies
# ------------------------------------------------------------------------------
FROM registry.access.redhat.com/ubi10/ubi-minimal:10.1@sha256:c858c2eb5bd336d8c400f6ee976a9d731beccf3351fa7a6f485dced24ae4af17 AS builder

ARG APP_DIR=/app
ARG OPENSHIFT_PYTHON_WRAPPER_COMMIT=''
ARG OPENSHIFT_PYTHON_UTILITIES_COMMIT=''

ENV UV_PYTHON=python3.12
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_SYNC=1
ENV UV_NO_CACHE=1

RUN microdnf -y install \
  libxml2-devel \
  libcurl-devel \
  openssl \
  openssl-devel \
  gcc \
  clang \
  git \
  python3-devel \
  && microdnf clean all

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR ${APP_DIR}

COPY cli cli
COPY docs docs
COPY utilities utilities
COPY tests tests
COPY libs libs
COPY exceptions exceptions
COPY README.md pyproject.toml uv.lock conftest.py pytest.ini ./

RUN mkdir -p ${APP_DIR}/output

RUN uv sync --locked \
  && if [ -n "${OPENSHIFT_PYTHON_WRAPPER_COMMIT}" ]; then uv pip install "git+https://github.com/RedHatQE/openshift-python-wrapper.git@${OPENSHIFT_PYTHON_WRAPPER_COMMIT}"; fi \
  && if [ -n "${OPENSHIFT_PYTHON_UTILITIES_COMMIT}" ]; then uv pip install "git+https://github.com/RedHatQE/openshift-python-utilities.git@${OPENSHIFT_PYTHON_UTILITIES_COMMIT}"; fi \
  && find ${APP_DIR}/ -type d -name "__pycache__" -print0 | xargs -0 -r rm -rfv \
  && rm -rf ${APP_DIR}/.cache

# ------------------------------------------------------------------------------
# Stage 2: runtime - clean image with only runtime dependencies
# ------------------------------------------------------------------------------
FROM registry.access.redhat.com/ubi10/ubi-minimal:10.1@sha256:c858c2eb5bd336d8c400f6ee976a9d731beccf3351fa7a6f485dced24ae4af17

ARG APP_DIR=/app

# Runtime shared libraries required by compiled Python extensions:
#   libxml2  - lxml
#   libcurl  - pycurl (libcurl-minimal from the base image provides libcurl.so)
#   openssl  - cryptography / TLS
RUN microdnf -y install \
  python3 \
  libxml2 \
  openssl \
  && microdnf clean all

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV JUNITFILE=${APP_DIR}/output/
ENV UV_PYTHON=python3.12
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_SYNC=1
ENV UV_NO_CACHE=1
# Prevents Python from writing .pyc files to disk
ENV PYTHONDONTWRITEBYTECODE=1
# Ensures Python output is logged straight to the terminal (useful for OpenShift logs)
ENV PYTHONUNBUFFERED=1

WORKDIR ${APP_DIR}

COPY --from=builder ${APP_DIR} ${APP_DIR}

RUN chgrp -R 0 ${APP_DIR} && chmod -R g=u ${APP_DIR}

USER 1001

CMD ["uv", "run", "pytest", "--collect-only"]
