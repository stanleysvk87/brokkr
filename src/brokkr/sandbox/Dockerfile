FROM debian:bookworm-slim

# Non-root user the sandboxed commands run as. The bind mount is already a
# scoped directory, but least privilege still applies inside the container
# itself -- nothing here should ever need root.
RUN groupadd --gid 10001 brokkr \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash brokkr

# Minimal, common tooling -- deliberately not "everything", since
# "everything" defeats the point of a sandbox. Extend this list as real
# proposed commands need specific tools; each addition should be a
# deliberate, reviewed change, not a reflexive "just in case".
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        coreutils \
        bash \
        curl \
        git \
        python3 \
        python3-pip \
        procps \
    && rm -rf /var/lib/apt/lists/*

USER brokkr
WORKDIR /workspace

# Keeps the container alive between `docker exec` calls -- see
# sandbox/docker_sandbox.py's module docstring for why this is one
# long-lived container rather than `docker run --rm` per command.
CMD ["sleep", "infinity"]
