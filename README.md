# cellphone-camera-rtsp-ingestor

Two components that let a browser run VLM inference over a **cellphone camera**
RTSP/MJPEG stream, routed through the **llm-d** router gateway.

```
  Browser ──ws──► frontend ──POST (SSE)──► llm-d router gateway
                     ▲                          │
                     │                          │ gateway hook provisions
                     └──── tokens ──────────────┤ a handler for the session
                                                ▼
                cellphone camera ──frames──► handler ──► inference pool
```

| | `frontend/` | `handler/` |
|---|---|---|
| Lifetime | long-lived Deployment | **short-lived**, one per session |
| Created by | you (manifest) | the llm-d router's gateway hook |
| Listens on | `:8080` (HTTP + WebSocket) | nothing — it only dials out |
| Talks to | the gateway, only | the cellphone camera stream + its assigned pool |
| Deps | FastAPI, uvicorn, httpx | OpenCV, numpy, httpx |
| Image | `<user>/cellphone-camera-frontend` | `<user>/cellphone-camera-handler` |

**Only the handler ever touches the camera stream.** The frontend takes the
cellphone camera URL from the client and passes it along as a value in its
POST; it never opens that connection, which is why its image has no OpenCV in
it. The only data the frontend receives is the token response to its own POST.

They are separate uv projects with separate lockfiles and Dockerfiles, built
from separate contexts. Neither imports the other — the only thing they share
is the wire contract described below.

## The session contract

The trigger provisions the handler. Results come back separately, to the
frontend replica that asked for them.

1. **Frontend → gateway** (`frontend/cellphone_camera_frontend/gateway.py`) — a short
   chat completion (`stream: false`, `max_tokens: 1`; nobody reads the answer,
   it exists to be routed and inspected), plus:
   - header `x-llmd-session-id: cellphone-camera-<uuid>` — the session key
   - header `x-llmd-frame-source: frontend-trigger` — "this is a trigger, not frame traffic"
   - header `x-cellphone-camera-stream-url` and body `cellphone-camera.{stream_url,prompt,interval}` — what the hook injects into the handler pod
   - header `x-cellphone-camera-results-callback: http://<this pod's IP>:8080/ingest` — **where to send the results**
2. **Gateway hook** (not in this repo) — reads those, creates a handler with
   `SESSION_ID`, `STREAM_URL`, `POOL_ENDPOINT`, `RESULTS_CALLBACK_URL`,
   `PROMPT`, `FRAME_INTERVAL`. It appends the session id to the callback, so
   the handler gets `.../ingest/<session-id>`. The trigger returns as soon as
   the handler exists.
3. **Handler → pool** (`handler/cellphone_camera_handler/pool.py`) — one request per
   sampled frame, straight to the pod llm-d assigned, bypassing the gateway.
   That bypass is the point: every frame in a session lands on the same pod, so
   the session's constant prompt stays warm in that pod's prefix cache.
4. **Handler → frontend → browser** — the handler pipes each frame's response,
   unbuffered, to `RESULTS_CALLBACK_URL`; the frontend decodes the SSE into
   per-frame token runs and relays them over the WebSocket. When the handler
   exits it POSTs a final `x-cellphone-camera-session-end`, which is the only
   way the frontend learns the session is over.

### Why the results do not come back on the trigger

They used to be meant to, on the theory that the gateway would join the
handler's output onto the caller's open stream. Nothing can do that join. The
handler posts frames straight to the assigned pod, so the gateway never
observes the response — and an Envoy ext_proc filter cannot splice one
request's response into another request's stream even in principle. The
callback is the return path, and it is the only one available.

### Why the callback is this pod's IP and not the Service

The frontend runs two replicas and a session's WebSocket lives entirely in one
replica's memory. Results sent to the Service would be load-balanced, and about
half would arrive at a replica that has never heard of the session — it answers
`404` and the tokens are lost. Only the replica holding the socket can name
itself, so it does, via `POD_IP` from the downward API.

The frontend still never learns the handler's address and never contacts it;
the handler contacts the frontend. **If your gateway correlates on something
other than `x-llmd-session-id`, `gateway.py` and `pool.py` are the only two
files to change.**

### Frame boundaries

One callback POST is one frame, and the handler labels it with
`x-cellphone-camera-frame-index`. The frontend no longer has to infer
boundaries from `finish_reason` in a single long stream — the request itself is
the boundary, which is why `FrameDecoder` carries no cross-frame state.

### Stopping

There is still no control channel into the handler pod — nothing can reach it
directly. **Stop** in the UI (and closing the browser tab) now sends a
`x-llmd-frame-source: frontend-stop` trigger through the gateway, whose hook
deletes the handler's Job. That is best effort: if the gateway is unreachable
the handler keeps reading the camera until one of its own backstops trips, so
always set them:

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
uv run cellphone-camera-frontend      # http://localhost:8080
```

Then open the UI, enter the cellphone camera URL, and hit **Start inference**.

- MJPEG: `http://<phone-ip>:4747/video`
- RTSP (app-dependent): `rtsp://<phone-ip>:4747/`

`GET /healthz` returns the resolved gateway URL for a connectivity sanity check.

To exercise the handler by hand — the gateway hook normally supplies all of
this — set the same values it would:

```bash
cd handler
SESSION_ID=local-dev \
STREAM_URL=http://192.168.1.42:4747/video \
POOL_ENDPOINT=http://vlm-service.default.svc.cluster.local:8000 \
RESULTS_CALLBACK_URL=http://127.0.0.1:8080/ingest/local-dev \
MAX_FRAMES=3 \
  uv run cellphone-camera-handler
```

See `frontend/.env.example` and `handler/.env.example` for every variable.

## Build

```bash
docker build -t cellphone-camera-frontend ./frontend
docker build -t cellphone-camera-handler  ./handler
```

`RESPONSE_TIMEOUT` (default 60s) now bounds only the trigger request, which
returns as soon as the handler is provisioned. It no longer caps how long a
session may run — results arrive on their own connections to `/ingest`, so the
handler's `MAX_SESSION_SECONDS` (default 300s) is the only session ceiling.

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
hook stamps out, with `${SESSION_ID}`, `${STREAM_URL}`, `${POOL_ENDPOINT}` and
`${RESULTS_CALLBACK_URL}` for the hook to substitute, and `rbac.yaml` grants the hook permission to
create them.

## Status

The handler here is a **POC stand-in** — a deliberately simple sample-and-submit
loop, meant to be replaced by a more robust RTSP handler later. The part worth
keeping is the session contract in `gateway.py` / `pool.py`.

Known rough edges in the results path:

- **A session outlives the replica holding it.** If that pod is evicted or
  rolled, its sessions die with it and the handler's callbacks start returning
  404 until its backstops trip. The PodDisruptionBudget and the 120s
  termination grace limit the blast radius; they do not remove it.
- **Backpressure is per-session, not global.** Each session buffers at most 256
  events before the handler's POST blocks, which is the behaviour we want, but
  nothing caps how many sessions one replica accepts.
