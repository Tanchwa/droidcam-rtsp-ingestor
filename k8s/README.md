# Kubernetes manifests

Two components, two very different shapes.

| | `frontend/` | `handler/` |
|---|---|---|
| Object | `Deployment` + `Service` + `HTTPRoute` | a `Job`, one per session |
| Created by | you, `kubectl apply` | the llm-d router's gateway hook |
| Lifetime | long-lived | seconds to minutes, then gone |
| Listens on | `:8080` | nothing |

**The handler has no Deployment and no Service on purpose.** It has no listener,
nothing ever connects to it, and it exits once its backstops fire — as a
Deployment it would crash-loop forever. What lives here instead is a Job
template in a ConfigMap (`handler/configmap-job-template.yaml`) that the
gateway hook reads and stamps out per session, plus the RBAC the hook needs to
do that.

```
k8s/
├── namespace.yaml
├── frontend/
│   ├── configmap.yaml            non-secret settings (gateway endpoint, model)
│   ├── secret.example.yaml       ← EXAMPLE, gateway API_KEY
│   ├── deployment.yaml           2 replicas, non-root, read-only rootfs
│   ├── service.yaml              ClusterIP :80 → :8080
│   ├── httproute.yaml            agentgateway route; WebSocket rule has no timeout
│   ├── gateway.example.yaml      ← EXAMPLE, only if you have no Gateway yet
│   └── pdb.yaml                  keep 1 replica up during drains
└── handler/
    ├── configmap-defaults.yaml       model, sampling, backstops
    ├── configmap-job-template.yaml   the per-session Job the hook stamps out
    ├── rbac.yaml                     SA for handler pods + Role for the hook
    └── example-job.yaml          ← EXAMPLE, run one handler by hand
```

The three `example` files are references, not part of the deploy. Don't bulk
apply the tree.

## Before you apply

Four things are placeholders and will not work as shipped:

1. **Image repo** — `docker.io/YOUR_DOCKERHUB_USERNAME/...` in
   `frontend/deployment.yaml`, `handler/configmap-job-template.yaml`, and
   `handler/example-job.yaml`. Set to the `DOCKERHUB_USERNAME` your CI
   publishes under.
2. **`GATEWAY_ENDPOINT`** in `frontend/configmap.yaml` — your llm-d router.
3. **`hostnames`** in `frontend/httproute.yaml`, and the `parentRefs` Gateway.
   We run agentgateway (`gatewayClassName: agentgateway`). Check
   `kubectl get gateways -A` first; if the agentgateway quickstart or llm-d
   already left one, attach to it and skip `gateway.example.yaml`.
4. **The RoleBinding subject** in `handler/rbac.yaml` — the ServiceAccount your
   gateway hook actually runs as. `llm-d-gateway-hook`/`llm-d` is a guess.

## Apply

The namespace has to exist before anything in it, so this is two commands, not
a recursive apply:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/frontend/configmap.yaml \
               -f k8s/frontend/deployment.yaml \
               -f k8s/frontend/service.yaml \
               -f k8s/frontend/httproute.yaml \
               -f k8s/frontend/pdb.yaml \
               -f k8s/handler/configmap-defaults.yaml \
               -f k8s/handler/configmap-job-template.yaml \
               -f k8s/handler/rbac.yaml
```

If the gateway needs auth, create the Secret out of band so no token lands in
git:

```bash
kubectl -n droidcam create secret generic droidcam-frontend-secrets \
  --from-literal=API_KEY='sk-...'
```

Both `secretRef`s are `optional: true`, so skip that step entirely for an
unauthenticated gateway.

## Pin an image

CI tags every build `latest` and `sha-<full-commit-sha>`. Pin the real one:

```bash
kubectl -n droidcam set image deploy/droidcam-frontend \
  frontend=YOUR_DOCKERHUB_USERNAME/droidcam-frontend:sha-$(git rev-parse HEAD)
```

The handler's tag lives in the ConfigMap template, so it changes by editing
`handler/configmap-job-template.yaml` and re-applying — no restart needed,
the hook re-reads it for the next session.

## Timeouts must stay ordered

Three numbers in three files, and they have to nest:

```
handler MAX_SESSION_SECONDS (300)   < frontend RESPONSE_TIMEOUT (600)
        configmap-defaults.yaml             frontend/configmap.yaml

handler MAX_SESSION_SECONDS (300)   < Job activeDeadlineSeconds (360)
                                            configmap-job-template.yaml
```

If `RESPONSE_TIMEOUT` drops below `MAX_SESSION_SECONDS`, the frontend closes
the browser's token feed while the handler is still submitting frames. If
`activeDeadlineSeconds` drops below it, Kubernetes kills healthy handlers
mid-session and you lose the real exit code.

The HTTPRoute's WebSocket rule sets `timeouts.request: 0s` for the same reason
— a proxy default of 30–60s would cut `/ws/inference` long before the session
ends. `timeouts` is standard-channel Gateway API v1.1+ and agentgateway honours
it natively, so no vendor annotation is needed.

That only removes the per-request deadline. If sessions still drop, the next
suspect is the connection-level HTTP/1 idle timeout, which agentgateway sets
via an `AgentgatewayPolicy` targeting the **Gateway** rather than the Route —
there is a ready-to-uncomment example at the bottom of `frontend/httproute.yaml`.

## Verify

```bash
kubectl -n droidcam rollout status deploy/droidcam-frontend
kubectl -n droidcam port-forward svc/droidcam-frontend 8080:80
curl -s localhost:8080/healthz    # echoes the resolved gateway URL
```

`/healthz` is a local check — it reports the configured gateway URL without
dialing it. That's deliberate: probes shouldn't kill a pod holding live
WebSocket sessions just because the upstream hiccuped. It also means a green
probe says nothing about gateway reachability; hit `/` and start a session for
that.

Confirm the hook's RBAC actually works:

```bash
kubectl auth can-i create jobs \
  --as=system:serviceaccount:llm-d:llm-d-gateway-hook -n droidcam
```

## Debug a session

Handler Jobs are labelled with their session id:

```bash
kubectl -n droidcam get jobs -l app.kubernetes.io/name=droidcam-handler
kubectl -n droidcam logs -l droidcam.io/session-id=<session-id> --tail=100
kubectl -n droidcam delete job -l droidcam.io/session-id=<session-id>
```

Exit codes: `0` completed, `1` the stream never produced frames, `2`
misconfigured. Finished Jobs self-delete 300s after completion
(`ttlSecondsAfterFinished`), so grab logs before then.

## Network reachability

`STREAM_URL` is whatever the user typed into the UI — usually a phone on their
LAN, like `http://192.168.1.42:4747/video`. **The handler pod has to be able to
reach that address**, and on a remote cluster it generally cannot. Every such
session fails with exit 1 no matter how correct these manifests are. Nothing
here can fix that; it needs the camera and the cluster on a routable network.

No NetworkPolicy ships here, because the handler's egress is by definition
arbitrary user-supplied addresses. If your cluster default-denies egress, the
handler needs an allowance for the camera ranges you intend to support and for
its assigned `POOL_ENDPOINT`.
