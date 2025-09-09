FROM quay.io/fedora/fedora:41

ARG APP_DIR=/app

ENV JUNITFILE=${APP_DIR}/output/

ENV UV_PYTHON=python3.12
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_SYNC=1
ENV UV_CACHE_DIR=${APP_DIR}/.cache

RUN dnf -y install \
  libxml2-devel \
  libcurl-devel \
  openssl \
  openssl-devel \
  libcurl-devel \
  gcc \
  clang \
  python3-devel \
  && dnf clean all \
  && rm -rf /var/cache/dnf \
  && rm -rf /var/lib/dnf \
  && truncate -s0 /var/log/*.log && rm -rf /var/cache/yum

WORKDIR ${APP_DIR}

RUN mkdir -p ${APP_DIR}/output

COPY utilities utilities
COPY tests tests
COPY libs libs
COPY exceptions exceptions
COPY README.md pyproject.toml uv.lock conftest.py pytest.ini report.py ./

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ARG OPENSHIFT_PYTHON_WRAPPER_COMMIT=''
ARG OPENSHIFT_PYTHON_UTILITIES_COMMIT=''

RUN uv sync --locked\
  && if [ -n "${OPENSHIFT_PYTHON_WRAPPER_COMMIT}" ]; then uv pip install git+https://github.com/RedHatQE/openshift-python-wrapper.git@$OPENSHIFT_PYTHON_WRAPPER_COMMIT; fi \
  && if [ -n "${OPENSHIFT_PYTHON_UTILITIES_COMMIT}" ]; then uv pip install git+https://github.com/RedHatQE/openshift-python-utilities.git@$OPENSHIFT_PYTHON_UTILITIES_COMMIT; fi \
  && find ${APP_DIR}/ -type d -name "__pycache__" -print0 | xargs -0 rm -rfv \
  && rm -rf ${APP_DIR}/.cache

CMD ["uv", "run", "pytest", "--collect-only"]
