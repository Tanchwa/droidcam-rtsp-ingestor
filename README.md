# droidcam-rtsp-ingestor

Two components that let a browser run VLM inference over a **DroidCam**
RTSP/MJPEG stream, routed through the **llm-d** router gateway.

```
  Browser ──ws──► frontend ──POST (SSE)──► llm-d router gateway
                     ▲                          │
                     │                          │ gateway hook provisions
                     └──── tokens ──────────────┤ a handler for the session
                                                ▼
                        DroidCam ──frames──► handler ──► inference pool
```

| | `frontend/` | `handler/` |
|---|---|---|
| Lifetime | long-lived Deployment | **short-lived**, one per session |
| Created by | you (manifest) | the llm-d router's gateway hook |
| Listens on | `:8080` (HTTP + WebSocket) | nothing — it only dials out |
| Talks to | the gateway, only | the DroidCam stream + its assigned pool |
| Deps | FastAPI, uvicorn, httpx | OpenCV, numpy, httpx |
| Image | `<user>/droidcam-frontend` | `<user>/droidcam-handler` |

**Only the handler ever touches the camera stream.** The frontend takes the
DroidCam URL from the client and passes it along as a value in its POST; it
never opens that connection, which is why its image has no OpenCV in it. The
only data the frontend receives is the token response to its own POST.

They are separate uv projects with separate lockfiles and Dockerfiles, built
from separate contexts. Neither imports the other — the only thing they share
is the wire contract described below.

## The session contract

One POST does double duty. The frontend sends it when the user hits **Start
inference**; it provisions the handler *and* stays open as the token stream.

1. **Frontend → gateway** (`frontend/droidcam_frontend/gateway.py`) — a normal
   chat completion with `stream: true`, plus:
   - header `x-llmd-session-id: droidcam-<uuid>` — the session key
   - header `x-llmd-frame-source: frontend-trigger` — "this is a trigger, not frame traffic"
   - header `x-droidcam-stream-url` and body `droidcam.{stream_url,prompt,interval}` — what the hook injects into the handler pod
2. **Gateway hook** (not in this repo) — reads those, creates a handler with
   `SESSION_ID`, `STREAM_URL`, `POOL_ENDPOINT`, `PROMPT`, `FRAME_INTERVAL`.
3. **Handler → pool** (`handler/droidcam_handler/pool.py`) — one request per
   sampled frame, carrying the same `x-llmd-session-id` and
   `x-llmd-frame-source: droidcam-handler`, so the gateway joins the output onto
   the caller's open stream from step 1.
4. **Gateway → frontend → browser** — tokens arrive on the step-1 response; the
   frontend splits them into per-frame blocks and relays them over the
   WebSocket. This response is the frontend's only inbound data.

The frontend never learns the handler's address and never contacts it. The
handler never contacts the frontend. **If your gateway correlates on something
other than `x-llmd-session-id`, `gateway.py` and `pool.py` are the only two
files to change.**

### Frame boundaries

The handler tags each submission with `frame_index`. If the gateway echoes it
back, the UI uses it; otherwise the frontend counts completions locally
(`finish_reason` ends a block). Either way you get one output block per frame.

### Stopping

There is no control channel into the handler pod — nothing can reach it. **Stop**
in the UI only closes the browser's token feed; the handler goes on reading the
camera stream regardless. It exits on its own backstops, so always set them:

| env var | default | effect |
|---|---|---|
| `MAX_SESSION_SECONDS` | `300` | wall-clock ceiling; `0` disables |
| `MAX_FRAMES` | `0` (unlimited) | frame ceiling |
| `IDLE_TIMEOUT` | `30` | give up if the stream yields no frames |

It also handles `SIGTERM`, so deleting the pod ends it cleanly. Exit codes:
`0` completed, `1` stream never produced frames, `2` misconfigured.

## Run it locally

Each component is its own uv project, so `uv run` from inside its directory:

```bash
# terminal 1 — the UI
cd frontend
cp .env.example .env          # point GATEWAY_ENDPOINT at your gateway
uv run droidcam-frontend      # http://localhost:8080
```

Then open the UI, enter the DroidCam URL, and hit **Start inference**.

- MJPEG: `http://<phone-ip>:4747/video`
- RTSP (DroidCamX): `rtsp://<phone-ip>:4747/`

`GET /healthz` returns the resolved gateway URL for a connectivity sanity check.

To exercise the handler by hand — the gateway hook normally supplies all of
this — set the same values it would:

```bash
cd handler
SESSION_ID=local-dev \
STREAM_URL=http://192.168.1.42:4747/video \
POOL_ENDPOINT=http://vlm-service.default.svc.cluster.local:8000 \
MAX_FRAMES=3 \
  uv run droidcam-handler
```

See `frontend/.env.example` and `handler/.env.example` for every variable.

## Build

```bash
docker build -t droidcam-frontend ./frontend
docker build -t droidcam-handler  ./handler
```

Keep the frontend's `RESPONSE_TIMEOUT` (default 600s) above the handler's
`MAX_SESSION_SECONDS` (default 300s), or tokens stop reaching the browser while
the handler is still submitting frames.

CI (`.github/workflows/docker-publish.yml`) builds both on every push to
`main` via a matrix, tagging each `latest` and the commit SHA. The two images
version together but deploy independently: the frontend as a Deployment, the
handler as whatever your gateway hook creates.

## Deploy

Manifests live in [`k8s/`](k8s/) — see [`k8s/README.md`](k8s/README.md) for the
placeholders you must fill in first.

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/frontend/configmap.yaml -f k8s/frontend/deployment.yaml \
               -f k8s/frontend/service.yaml -f k8s/frontend/httproute.yaml \
               -f k8s/frontend/pdb.yaml \
               -f k8s/handler/configmap-defaults.yaml \
               -f k8s/handler/configmap-job-template.yaml -f k8s/handler/rbac.yaml
```

The frontend is a Deployment behind a ClusterIP Service and a Gateway API
HTTPRoute. The handler is **not** a Deployment and has no Service — it has no
listener and exits on its backstops, so it would only crash-loop. Instead
`k8s/handler/configmap-job-template.yaml` holds the per-session Job the gateway
hook stamps out, with `${SESSION_ID}`, `${STREAM_URL}` and `${POOL_ENDPOINT}`
for the hook to substitute, and `rbac.yaml` grants the hook permission to
create them.

## Status

The handler here is a **POC stand-in** — a deliberately simple sample-and-submit
loop, meant to be replaced by a more robust RTSP handler later. The part worth
keeping is the session contract in `gateway.py` / `pool.py`.
