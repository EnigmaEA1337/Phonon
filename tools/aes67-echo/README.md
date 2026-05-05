# aes67-echo — multicast echo container

Test helper for validating AES67 send/receive on a single host. Receives
RTP packets on `IN_GROUP:IN_PORT` and re-emits them byte-for-byte on
`OUT_GROUP:OUT_PORT`. Useful as a stand-in for a second Stage when you
have only one machine.

## Why this exists

PipeWire's `module-rtp-sink` defaults to `IP_MULTICAST_LOOP=0` (correct
AES67 behaviour — sender shouldn't hear itself). On a single-host dev
setup that means a local `module-rtp-source` never receives its own
sender's packets. This container bounces the stream out and back so the
loop closes on one box.

## Build & run

```bash
cd tools/aes67-echo
docker compose up --build -d
docker compose logs -f
```

Stop:

```bash
docker compose down
```

## Override the multicast groups

```bash
IN_GROUP=239.69.10.10 IN_PORT=5004 \
OUT_GROUP=239.69.10.20 OUT_PORT=5004 \
docker compose up -d
```

## Notes

- Runs in `network_mode: host` — multicast doesn't traverse Docker bridges.
- For PipeWire's sender to be received locally you also need `net.loop = true`
  in the `module-rtp-sink` args (this is what the Phonon Stage AES67 API
  sets when `loop=true` is passed in the create request).
- No transcoding: payload preserved as-is. Works for L16, L24, or any
  other RTP payload.
