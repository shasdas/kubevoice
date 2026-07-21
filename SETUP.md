# KubeVoice — Build Log & Setup Guide

This is both a working setup guide (follow it top to bottom to reproduce the
environment) and a day-by-day log of how KubeVoice actually got built —
including the snags hit along the way and how each was diagnosed and fixed.
Real debugging is part of the story here, not something to hide.

Stack: Docker, kubectl, kind, Helm, LiveKit Agents (Python, uv-managed), the
`kubernetes` Python client. Environment: Windows 11 + WSL2 (Ubuntu).

---

## Day 1 — Environment, and the agent's first spoken words

### Install the tools
- **Docker Desktop** with the WSL2 backend (Windows), used from inside a WSL2
  Ubuntu terminal.
- **kubectl**
  ```bash
  curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
  chmod +x kubectl && sudo mv kubectl /usr/local/bin/
  ```
- **kind**
  ```bash
  curl -Lo ./kind https://kind.sigs.k8s.io/dl/latest/kind-linux-amd64
  chmod +x ./kind && sudo mv ./kind /usr/local/bin/kind
  ```
- **Helm**
  ```bash
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
  ```

### Scaffold the agent
Started from LiveKit's official template rather than from scratch:
```bash
lk agent init kubevoice --template agent-starter-python
```

### Snag #1 — `console` mode can't find a microphone
Running `uv run python agent.py console` under WSL2 failed: PortAudio couldn't
locate an audio device. **Root cause:** a stock WSL2 Ubuntu shell has no ALSA
hardware devices to offer it — there's no sound card in a Linux subsystem.

**Fix — and a better mental model, not just a workaround:** `console` mode is
a local-terminal convenience; it's not how the agent talks to anyone in
production. Switched to `dev` mode instead, which registers the worker with
LiveKit Cloud over plain WebSocket — no local audio needed at all — and did
all voice I/O from a browser on the Windows host (LiveKit's hosted
agent-console / playground), which has real mic and speaker access.
```bash
uv run python agent.py dev
```
This is also the *correct* production topology: a deployed worker has no
microphone either. Console mode never got revisited.

### Snag #2 — worker process failing to spawn (`TimeoutError`, exit code -10)
`dev` mode registered fine with LiveKit Cloud, but every job dispatch failed:
the child process that runs the agent logic timed out during initialization
and was killed (`SIGUSR1`, exit -10), three attempts, then gave up.

**Root cause:** the project lived on the Windows filesystem, mounted into
WSL2 at `/mnt/c/...`. File I/O across that 9P/drvfs boundary is dramatically
slower than native Linux disk — and process init (importing `livekit-agents`,
`av`, loading the Silero VAD and turn-detector model weights) is exactly the
worst case for it, easily blowing past the framework's init timeout.

**Fix:** moved the entire project into the native WSL2 filesystem and rebuilt
the venv there.
```bash
rsync -a --exclude .venv /mnt/c/Users/<you>/explore-livekit/ ~/kubevoice/agent/
cd ~/kubevoice/agent
uv sync
uv run python src/agent.py download-files   # pre-fetch VAD/turn-detector models
uv run python src/agent.py dev
```
Edited from then on via VS Code's **Remote - WSL** extension (`code ~/kubevoice`
from the WSL shell) — Windows-side editor UI, Linux-side files and runtime.
Lesson banked for later: the editor can live on Windows, the runtime should
not.

**Result:** worker registered, job dispatched, process spawned successfully.
First spoken words from the agent — the Day 1 milestone.

---

## Day 2 — Kubernetes tools, and the agent's first real diagnosis

### Create the cluster
```bash
kind create cluster --config demo-cluster/kind-config.yaml
kubectl get nodes   # kubevoice-control-plane, kubevoice-worker — both Ready
```

### Seed it with real (and deliberately broken) workloads
```bash
kubectl apply -f demo-cluster/seed/demo-workloads.yaml
```
Three namespaces (`payments`, `orders`, `monitoring`) with healthy nginx/busybox
stand-ins, plus two **deliberately broken** workloads so the agent has
something meaningful to find:
- `payments/payments-worker` → bad image tag → `ImagePullBackOff`
- `orders/orders-batch` → container exits immediately → `CrashLoopBackOff`

### Build the tools
Implemented function tools against the official `kubernetes` Python client —
pod listing, deployment status, recent events, node health — using the
try-in-cluster / fall-back-to-local pattern:
```python
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()
```

### First real result
Asked the agent (via console, in the browser) for a cluster health report. It
correctly found and spoke both seeded failures:

> "Two pods have issues — 'orders-batch...' in the 'orders' namespace is in
> Error state, and 'payments-worker...' in 'payments' is facing an
> ImagePullBackOff. Would you like me to gather recent events to investigate
> further?"

Voice → tool call → live cluster truth → spoken summary, working end to end.

### Two things the logs immediately flagged for follow-up
1. **The tool returned the full raw Kubernetes API response** (complete with
   `managedFields`, owner references, every timestamp) straight to the LLM —
   it worked, but at the cost of the model parsing tens of KB of noise to say
   two sentences.
2. **`silero — inference is slower than realtime` (0.83s delay)** — the
   synchronous `kubernetes` client was blocking the event loop during API
   calls, starving the voice-activity detector of CPU time.

Both addressed iteratively (see *Design decisions* in the main README):
tool outputs reshaped into small pre-summarized structures, and blocking
Kubernetes calls wrapped in `asyncio.to_thread(...)` so they run off the
event loop.

---

## Day 3 — Voice UX pass

Small behavioral changes that matter more than their size suggests:
- **Acknowledge before slow work** — the agent says "let me check the
  cluster" before a tool call, so a 1–2 second lookup doesn't read as a stall.
- **Pronounceable answers** — referring to workloads by deployment + namespace
  ("the orders-batch deployment in orders") rather than reading out
  replica-set-hash pod names or ISO-8601 timestamps aloud.
- **Interruption handling** — verified barge-in (built into the framework)
  actually stops the agent mid-sentence gracefully.
- **Graceful degradation** — tool exceptions (e.g. cluster unreachable) turn
  into a spoken fallback rather than silence or a crash.
- **Follow-up continuity** — confirmed a "yes, gather those events" follow-up
  correctly reuses the namespace/context from the prior turn.

---

## Day 4 — Containerizing the agent

### Reference Dockerfile
`agent/Dockerfile` — multi-stage, `uv`-based, non-root final user, model
files pre-downloaded at build time so pods start fast.

### Snag #3 — `docker build` failing on `download-files`
```
kubernetes.config.config_exception.ConfigException: Service host/port is not set.
...
kubernetes.config.config_exception.ConfigException: Invalid kube-config file. No configuration found.
```
**Root cause:** the Kubernetes config-loading snippet from Day 2 sat at
**module level** in `agent.py`. That meant it ran the instant the file was
*imported* — including for the unrelated `download-files` subcommand, during
`docker build`, where obviously no cluster or kubeconfig exists yet.

**Fix:** made the config load lazily, on first actual use by a tool, instead
of at import time:
```python
_k8s_loaded = False

def _ensure_k8s_config():
    global _k8s_loaded
    if _k8s_loaded:
        return
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    _k8s_loaded = True
```
A good general lesson, independent of Docker: don't do environment-dependent
work at import time.

### Build and load into kind
```bash
docker build -t kubevoice-agent:0.1.0 .
kind load docker-image kubevoice-agent:0.1.0 --name kubevoice
```
(`kind load` is the step that's easy to forget — kind nodes can't see local
Docker images otherwise, and you'll see `ErrImagePull` on your own image if
you skip it.)

---

## Day 5 — Deploying with Helm, and the agent answering from inside its own cluster

### Namespace and secrets
```bash
kubectl create namespace kubevoice
kubectl -n kubevoice create secret generic kubevoice-secrets --from-env-file=.env
```

### Install
```bash
helm template kubevoice deploy/kubevoice --namespace kubevoice | less   # sanity check first
helm install kubevoice deploy/kubevoice --namespace kubevoice
```

### Snag #4 — CrashLoopBackOff: `Permission denied` resolving the Python interpreter
```
error: Failed to query Python interpreter
  Caused by: failed to canonicalize path `/app/.venv/bin/python3`: Permission denied (os error 13)
```
**Root cause:** the project's Python version requirement didn't match the
base image's system interpreter exactly, so `uv sync` downloaded its own
managed Python build during the (root) build stage into a cache directory
outside `/app`. The Dockerfile's `chown -R agent:agent /app` never touched
that directory — so once the container switched to the non-root `agent`
user, it couldn't read its own interpreter.

**Fix:** pin `uv`'s managed-Python cache to a location that gets explicitly
chowned:
```dockerfile
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN mkdir -p /opt/uv/python
...
RUN useradd --create-home agent \
    && chown -R agent:agent /app /opt/uv/python
USER agent
```
Rebuild, reload, restart:
```bash
docker build -t kubevoice-agent:0.1.0 .
kind load docker-image kubevoice-agent:0.1.0 --name kubevoice
kubectl -n kubevoice rollout restart deploy/kubevoice
```

### Result
```bash
kubectl -n kubevoice get pods                 # Running, 0 restarts
kubectl -n kubevoice logs -f deploy/kubevoice  # "registered worker" — connected to LiveKit Cloud
```
Connected from the browser console and asked the deployed agent the same
questions as Day 2 — now answered by the instance running **inside the
cluster it was reporting on**, over strictly read-only RBAC. This is the
demo's money moment.

---

## Reproducing this environment from scratch

```bash
# 1. Cluster
kind create cluster --config demo-cluster/kind-config.yaml
kubectl apply -f demo-cluster/seed/demo-workloads.yaml

# 2. Local dev loop (fast iteration on tools/prompt)
cd agent
uv sync
uv run python src/agent.py download-files
uv run python src/agent.py dev      # talk to it via LiveKit's agent console/playground

# 3. Containerize and deploy
docker build -t kubevoice-agent:0.1.0 .
kind load docker-image kubevoice-agent:0.1.0 --name kubevoice
kubectl create namespace kubevoice
kubectl -n kubevoice create secret generic kubevoice-secrets --from-env-file=.env
helm install kubevoice deploy/kubevoice --namespace kubevoice
kubectl -n kubevoice logs -f deploy/kubevoice
```

**Iterating after code changes:**
```bash
docker build -t kubevoice-agent:0.1.1 .          # bump the tag — same-tag rebuilds
kind load docker-image kubevoice-agent:0.1.1 --name kubevoice   # won't be picked up otherwise
helm upgrade kubevoice deploy/kubevoice -n kubevoice --set image.tag=0.1.1
```

**Tearing down:**
```bash
kind delete cluster --name kubevoice
```

**Health probe port mismatch** (if probes fail but logs look healthy — check
the actual port the worker's health server logs, then):
```bash
helm upgrade kubevoice deploy/kubevoice -n kubevoice --set health.port=<port>
# or, while debugging:
helm upgrade kubevoice deploy/kubevoice -n kubevoice --set health.enabled=false
```

---

## What's in this repo

```
agent/                  # LiveKit Agents worker (Python, uv-managed) + Dockerfile
demo-cluster/           # kind cluster config + seed manifests (incl. deliberately broken pods)
deploy/kubevoice/       # Helm chart: Deployment, read-only RBAC, health probes
SETUP.md                # this file
README.md               # overview, architecture, design decisions
```

The RBAC in the chart is deliberately read-only (`get`/`list`/`watch` on
pods, events, nodes, deployments — nothing else). The agent observes; it
never mutates. See the README's *Design decisions* and *What's next* for why,
and what a safe path to remediation would look like.