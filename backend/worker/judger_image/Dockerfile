FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ \
    python3 \
    default-jdk \
    time \
    coreutils \
    && rm -rf /var/lib/apt/lists/*

COPY run-judge /usr/local/bin/run-judge
RUN chmod 0755 /usr/local/bin/run-judge

RUN python3 --version && g++ --version && java -version && /usr/bin/time --version 2>&1 | head -1

WORKDIR /workspace
