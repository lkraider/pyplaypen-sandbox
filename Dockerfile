# Reference container. This library is defense in depth *inside* a container,
# never the trust boundary itself (see README). The parent may stay root so it
# can chown into bind-mounted artifact dirs; each call then drops to a dedicated
# UID before running code, which is what makes RLIMIT_NPROC bind on Linux, where
# root is exempt. Swap the COPY+install for `pip install pyplaypen-sandbox` to
# base your own image on this.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS base

RUN groupadd --gid 10001 sandbox && \
    useradd --no-create-home --uid 10001 --gid 10001 sandbox

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps . && chown -R 10001:10001 /app

# Test stage: run the suite on real Linux, where RLIMIT_AS/NPROC and the
# root-to-UID drop actually apply (they no-op on macOS). Run it as root to
# cover the privileged branch and as uid 10001 to cover the unprivileged one.
FROM base AS test
RUN pip install --no-cache-dir pytest pytest-asyncio
COPY tests/ ./tests/
RUN chown -R 10001:10001 /app/tests
CMD ["pytest", "-q"]
