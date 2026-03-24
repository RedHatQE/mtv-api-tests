FROM registry.access.redhat.com/ubi10/ubi-minimal:10.1

ARG APP_DIR=/app
ARG OPENSHIFT_PYTHON_WRAPPER_COMMIT=''
ARG OPENSHIFT_PYTHON_UTILITIES_COMMIT=''

ENV JUNITFILE=${APP_DIR}/output/

ENV UV_PYTHON=python3.12
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_SYNC=1
ENV UV_CACHE_DIR=${APP_DIR}/.cache
ENV UV_NO_CACHE=1
# Prevents Python from writing .pyc files to disk
ENV PYTHONDONTWRITEBYTECODE=1
# Ensures Python output is logged straight to the terminal (useful for OpenShift logs)
ENV PYTHONUNBUFFERED=1

RUN microdnf -y install \
  libxml2-devel \
  libcurl-devel \
  openssl \
  openssl-devel \
  gcc \
  clang \
  git \
  python3-devel \
  python3-pip \
  && microdnf clean all

RUN pip install --no-cache-dir uv==0.10.9

WORKDIR ${APP_DIR}


COPY docs docs
COPY utilities utilities
COPY tests tests
COPY libs libs
COPY exceptions exceptions
COPY README.md pyproject.toml uv.lock conftest.py pytest.ini ./

RUN mkdir -p ${APP_DIR}/output

RUN chgrp -R 0 ${APP_DIR} && chmod -R g=u ${APP_DIR}

USER 1001

RUN uv sync --locked\
  && if [ -n "${OPENSHIFT_PYTHON_WRAPPER_COMMIT}" ]; then uv pip install "git+https://github.com/RedHatQE/openshift-python-wrapper.git@${OPENSHIFT_PYTHON_WRAPPER_COMMIT}"; fi \
  && if [ -n "${OPENSHIFT_PYTHON_UTILITIES_COMMIT}" ]; then uv pip install "git+https://github.com/RedHatQE/openshift-python-utilities.git@${OPENSHIFT_PYTHON_UTILITIES_COMMIT}"; fi \
  && find ${APP_DIR}/ -type d -name "__pycache__" -print0 | xargs -0 rm -rfv \
  && rm -rf ${APP_DIR}/.cache

CMD ["uv", "run", "pytest", "--collect-only"]
