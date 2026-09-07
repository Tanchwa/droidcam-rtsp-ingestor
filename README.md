# droidcam-rtsp-ingestor

Web UI + backend that ingests a **DroidCam** RTSP/MJPEG stream, samples frames,
forwards each to a **VLM inference server**, and streams the model's responses
back to the browser live.

```
 Browser UI ──ws──► FastAPI server ──http──► VLM inference server
                         │                    (in-cluster or via Envoy gateway)
                         └── OpenCV pulls frames from the DroidCam URL
```

The UI exposes exactly three things: the DroidCam **URL** input, a **Start
inference** button, and a live **text output** of the VLM responses (plus a
prompt box and frame interval).

## Requirements

- Python ≥ 3.13, [`uv`](https://docs.astral.sh/uv/)
- The DroidCam app running on a phone on the same network
- An OpenAI-compatible VLM server (vLLM / SGLang / LMDeploy, e.g. serving
  `Qwen2-VL`) reachable at `VLM_ENDPOINT`

## Configure

Point the ingestor at your VLM server via environment variables (see
`.env.example`). The endpoint is a single variable so the same build runs
against an in-cluster service **or** a public Envoy gateway:

```bash
export VLM_ENDPOINT=http://vlm-service.default.svc.cluster.local:8000   # in-cluster
# or
export VLM_ENDPOINT=https://vlm.example.com                             # via Envoy
export VLM_MODEL=Qwen/Qwen2-VL-7B-Instruct
export VLM_API_KEY=...        # only if the gateway requires a bearer token
```

## Run

```bash
uv run main.py
# open http://localhost:8080
```

1. Enter the DroidCam stream URL, e.g.
   - MJPEG: `http://<phone-ip>:4747/video`
   - RTSP (DroidCamX): `rtsp://<phone-ip>:4747/`
2. (Optional) edit the prompt and the seconds-between-frames.
3. Click **Start inference**. Responses stream into the output panel, one block
   per sampled frame. Click **Stop** to end.

`GET /healthz` returns the resolved VLM URL for a quick connectivity sanity check.

## How it forwards to the VLM

Each sampled frame is downscaled, JPEG-encoded, base64-embedded as a `data:`
image URL, and POSTed to `${VLM_ENDPOINT}/v1/chat/completions` with
`stream=true`. Tokens are relayed to the browser over the WebSocket as they
arrive. The request body lives in `VLMClient._build_payload`
(`droidcam_ingestor/vlm.py`) — edit that one method to target a non-OpenAI API.
