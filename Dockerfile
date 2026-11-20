# Images of the build server: the API server and the build worker.
#
#   docker compose up -d --build                    (see compose.yaml)
#   docker build --target worker-test -t buildserver-test .
#   docker run --rm --init -e BUILDSERVER_TEST_SANDBOX=1 buildserver-test
#
# Ubuntu 22.04 because Vivado supports it. Vivado itself is not in the image:
# the host's Linux install is mounted read-only at /opt/Xilinx.

FROM ubuntu:22.04 AS base

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    # Python and the virtual environment live where every account can read
    # them: the build account runs the build command with this Python too
    # when the command is a Python script.
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates locales \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.27 /uv /usr/local/bin/uv

# app runs the server processes. builder runs build commands (worker only);
# both are in group build, which owns the work root.
RUN groupadd --gid 1000 app \
    && groupadd --gid 1001 build \
    && useradd --uid 1000 --gid app --groups build --no-create-home \
        --shell /usr/sbin/nologin app \
    && useradd --uid 1001 --gid build --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin builder

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv python install 3.12 \
    && uv sync --frozen --no-dev --no-install-project --python 3.12
COPY buildserver ./buildserver
RUN uv sync --frozen --no-dev --python 3.12

# Only app may enter the data directory: it holds the database (tokens,
# users), the packages, logs and artifacts. A new named volume takes this
# ownership and mode from the image.
RUN mkdir /data && chown app:app /data && chmod 0700 /data


FROM base AS api

USER app
EXPOSE 8000
# The API is only reachable from Caddy on the compose network, so the proxy
# headers of any peer are trusted.
CMD ["uvicorn", "--factory", "buildserver.api:create_app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]


FROM base AS worker

# git clones repositories; sudo and procps run and kill build commands as
# builder; the rest are the libraries Vivado needs on Ubuntu 22.04.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git sudo procps \
        libtinfo5 libncurses5 libx11-6 libxext6 libxrender1 libxtst6 \
        libxi6 libfreetype6 libfontconfig1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# app may run commands as builder, and nothing else.
COPY deploy/sudoers /etc/sudoers.d/buildserver
RUN chmod 0440 /etc/sudoers.d/buildserver && visudo -cf /etc/sudoers.d/buildserver

# The work root: app creates the work directories, builder writes in them.
# setgid keeps everything in group build.
RUN mkdir /work && chown app:build /work && chmod 2770 /work

ENV BUILDSERVER_DATA_DIR=/data \
    BUILDSERVER_WORK_DIR=/work \
    BUILDSERVER_BUILD_USER=builder

USER app
CMD ["python", "-m", "buildserver.worker"]


# The worker image with the tests and their dependencies, to run the test
# suite, including the sandbox tests, on Linux.
FROM worker AS worker-test

USER root
RUN uv sync --frozen --python 3.12
COPY tests ./tests
RUN chown -R app:app /app/tests
USER app
ENV BUILDSERVER_TEST_WORK_DIR=/work
CMD ["python", "-m", "pytest", "-q"]
