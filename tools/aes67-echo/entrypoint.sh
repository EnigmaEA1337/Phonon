#!/bin/sh
set -e

echo "AES67 echo: receiving from ${IN_GROUP}:${IN_PORT}, retransmitting on ${OUT_GROUP}:${OUT_PORT}"
echo "Fake Stage: ${FAKE_STAGE_ID} on HTTP :${FAKE_HTTP_PORT}"

# Start the gstreamer echo in the background. Pure UDP pass-through:
# loop=true on udpsink re-enables IP_MULTICAST_LOOP so a local PipeWire
# receiver in the same host namespace can pick up our packets.
gst-launch-1.0 -q \
    udpsrc multicast-group="${IN_GROUP}" port="${IN_PORT}" auto-multicast=true \
  ! udpsink host="${OUT_GROUP}" port="${OUT_PORT}" auto-multicast=true loop=true &

GST_PID=$!
trap "kill ${GST_PID} 2>/dev/null" EXIT INT TERM

# Run simulator (mDNS + HTTP API + SAP) in the foreground
exec python3 /simulator.py
