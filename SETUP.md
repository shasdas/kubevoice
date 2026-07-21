# KubeVoice Demo — Cluster & Helm Setup Guide

Step-by-step instructions to stand up the local Kubernetes cluster, seed it with
demo workloads, and deploy the LiveKit agent worker via Helm. Commands are for
Linux / WSL2 (bash). macOS notes are inline where they differ.

---

## Phase 0 — Install the tools

You need: Docker, kubectl, kind, and Helm.

### 0.1 Docker
- **Linux:** install Docker Engine per your distro (`sudo apt install docker.io` on
  Ubuntu, then `sudo usermod -aG docker $USER` and re-login).
- **Windows:** install Docker Desktop with the WSL2 backend, run everything below
  inside your WSL2 (Ubuntu) terminal.
- **macOS:** Docker Desktop, then use Terminal directly.

Verify:
```bash
docker version && docker run --rm hello-world
```

### 0.2 kubectl
```bash
curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl && sudo mv kubectl /usr/local/bin/
kubectl version --client
```
(macOS: `brew install kubectl`)

### 0.3 kind
```bash
# Check https://github.com/kubernetes-sigs/kind/releases for the latest version
curl -Lo ./kind https://kind.sigs.k8s.io/dl/latest/kind-linux-amd64
chmod +x ./kind && sudo mv ./kind /usr/local/bin/kind
kind version
```
(macOS: `brew install kind`)

### 0.4 Helm
```bash
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
helm version
```
(macOS: `brew install helm`)

---

## Phase 1 — Create the cluster

From the root of this starter kit:

```bash
kind create cluster --config demo-cluster/kind-config.yaml
```

This creates a two-node cluster named `kubevoice` (control-plane + worker) and
sets your kubectl context to `kind-kubevoice`. Verify:

```bash
kubectl cluster-info --context kind-kubevoice
kubectl get nodes
# Expect: kubevoice-control-plane and kubevoice-worker, both Ready
```

---

## Phase 2 — Seed the demo workloads

```bash
kubectl apply -f demo-cluster/seed/demo-workloads.yaml
```

This creates three namespaces (`payments`, `orders`, `monitoring`) with healthy
apps **plus two deliberately broken workloads**:

- `payments/payments-worker` → **ImagePullBackOff** (bad image tag)
- `orders/orders-batch` → **CrashLoopBackOff** (container exits immediately)

These give your voice agent something meaningful to find. Verify (wait a minute
or two for the failure states to develop):

```bash
kubectl get pods -A
kubectl get events -n payments --sort-by=.lastTimestamp | tail
```

You should see the two failing pods. This is the state your demo recording wants:
"Any problems in the cluster?" → *"Yes — one pod in payments can't pull its
image, and a batch job in orders is crash-looping."*

---

## Phase 3 — Build the agent (Days 1–3 of the project plan)

This guide assumes you've built the agent from LiveKit's starter template
(`lk agent init kubevoice --template agent-starter-python`) and added your
Kubernetes function tools. Two integration notes that matter for the cluster:

1. **Kubernetes client config:** in your tool code, load config so it works both
   locally and in-cluster:
   ```python
   from kubernetes import config
   try:
       config.load_incluster_config()   # running inside the cluster (Phase 5)
   except config.ConfigException:
       config.load_kube_config()        # running locally in console/dev mode
   ```
2. **Local testing first:** run `console` / `dev` mode on your laptop against the
   kind cluster (it uses your kubeconfig) before containerizing. Get the tools
   right before you touch Docker.

---

## Phase 4 — Build the image and load it into kind

kind nodes can't see your local Docker images unless you load them explicitly —
no registry needed:

```bash
# From your agent project directory (see agent/Dockerfile in this kit for reference)
docker build -t kubevoice-agent:0.1.0 .

kind load docker-image kubevoice-agent:0.1.0 --name kubevoice
```

`kind load` is the step people forget; if your pod shows `ErrImagePull` for your
own image later, this is why.

---

## Phase 5 — Deploy with Helm

### 5.1 Namespace and secrets

The worker needs your LiveKit + model-provider credentials. Create them from the
same `.env.local` you used in dev mode:

```bash
kubectl create namespace kubevoice
kubectl -n kubevoice create secret generic kubevoice-secrets \
  --from-env-file=.env.local
```

(`.env.local` must contain at least `LIVEKIT_URL`, `LIVEKIT_API_KEY`,
`LIVEKIT_API_SECRET`, plus your LLM/STT/TTS keys. Never commit this file; the
chart consumes the Secret by name. In the README, note that production would use
External Secrets / sealed-secrets instead.)

### 5.2 Sanity-check and install the chart

```bash
# Render templates locally first — catch errors before touching the cluster
helm template kubevoice deploy/kubevoice --namespace kubevoice | less

helm install kubevoice deploy/kubevoice --namespace kubevoice
```

### 5.3 Verify

```bash
kubectl -n kubevoice get pods
kubectl -n kubevoice logs -f deploy/kubevoice
```

Healthy startup looks like the worker registering with LiveKit Cloud
("registered worker" in the logs). It dials **out** to LiveKit — no Service,
Ingress, or port-forward is needed for voice traffic.

If the health probes fail but the logs look fine, your livekit-agents version
may serve health on a different port: check the logs for the health server
port, then upgrade with:

```bash
helm upgrade kubevoice deploy/kubevoice -n kubevoice --set health.port=<port>
# or disable probes while debugging:
helm upgrade kubevoice deploy/kubevoice -n kubevoice --set health.enabled=false
```

### 5.4 Talk to it

With the worker Running, connect from any frontend — the hosted sandbox
frontend from LiveKit Cloud is the fastest — and ask:

- "How many pods are running in the payments namespace?"
- "Are there any problems in the cluster?"
- "What's the status of the orders-batch deployment?"

The agent answering questions about the cluster it is running inside is your
demo's money moment — record it.

---

## Phase 6 — Iterating

```bash
# After code changes: rebuild, reload, restart
docker build -t kubevoice-agent:0.1.0 .
kind load docker-image kubevoice-agent:0.1.0 --name kubevoice
kubectl -n kubevoice rollout restart deploy/kubevoice

# Tear down / recreate the whole environment (fully reproducible)
kind delete cluster --name kubevoice
```

Tip: bump the image tag (0.1.1, 0.1.2 …) on each iteration instead of reusing
0.1.0 — kind caches images by tag and `IfNotPresent` will not pick up a
same-tag rebuild without a restart.

---

## What's in this kit

```
demo-cluster/kind-config.yaml        # 2-node kind cluster definition
demo-cluster/seed/demo-workloads.yaml# namespaces + healthy & broken apps
deploy/kubevoice/                    # Helm chart: Deployment, read-only RBAC, probes
agent/Dockerfile                     # reference Dockerfile for the worker image
SETUP.md                             # this guide
```

The RBAC in the chart is deliberately read-only (get/list/watch on pods,
events, nodes, deployments…). Keep it that way and say so in your README —
"the agent can observe but never mutate" is a design decision interviewers
will respect.
