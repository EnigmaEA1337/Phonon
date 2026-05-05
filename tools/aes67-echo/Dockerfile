FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-good \
    python3 \
    python3-aiohttp \
    python3-zeroconf \
 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
COPY simulator.py /simulator.py
RUN chmod +x /entrypoint.sh

ENV IN_GROUP=239.69.10.10 \
    IN_PORT=5004 \
    OUT_GROUP=239.69.10.20 \
    OUT_PORT=5004 \
    FAKE_STAGE_ID=stage-echo01 \
    FAKE_HTTP_PORT=8402

ENTRYPOINT ["/entrypoint.sh"]
