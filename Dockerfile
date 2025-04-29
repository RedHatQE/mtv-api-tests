FROM quay.io/fedora/fedora:41
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG APP_DIR=/app

ENV KUBECONFIG=/cred/kubeconfig
ENV JUNITFILE=${APP_DIR}/output/

ENV UV_PYTHON=python3.12
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_SYNC=1
ENV UV_CACHE_DIR=${APP_DIR}/.cache

RUN dnf -y --disableplugin=subscription-manager install \
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

RUN mkdir /cred && mkdir -p ${APP_DIR}/output

COPY utilities utilities
COPY tests tests
COPY scripts scripts
COPY libs libs
COPY README.md pyproject.toml uv.lock conftest.py pytest.ini report.py ./

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN chmod +x scripts/run-tests.sh

ARG OPENSHIFT_PYTHON_WRAPPER_COMMIT=''
ARG OPENSHIFT_PYTHON_UTILITIES_COMMIT=''

RUN uv sync --locked\
  && if [ -n "${OPENSHIFT_PYTHON_WRAPPER_COMMIT}" ]; then uv pip install git+https://github.com/RedHatQE/openshift-python-wrapper.git@$OPENSHIFT_PYTHON_WRAPPER_COMMIT; fi \
  && if [ -n "${OPENSHIFT_PYTHON_UTILITIES_COMMIT}" ]; then uv pip install git+https://github.com/RedHatQE/openshift-python-utilities.git@$OPENSHIFT_PYTHON_UTILITIES_COMMIT; fi \
  && uv export --no-hashes \
  && find ${APP_DIR}/ -type d -name "__pycache__" -print0 | xargs -0 rm -rfv \
  && rm -rf ${APP_DIR}/.cache

CMD ["./scripts/run-tests.sh"]
