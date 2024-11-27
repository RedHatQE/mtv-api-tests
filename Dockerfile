FROM registry.access.redhat.com/ubi9/python-312:latest

USER root

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

RUN uv sync \
  && uv export --no-hashes \
  && find ${APP_DIR}/ -type d -name "__pycache__" -print0 | xargs -0 rm -rfv

RUN rm -rf ${APP_DIR}/.cache

CMD ["./scripts/run-tests.sh"]
