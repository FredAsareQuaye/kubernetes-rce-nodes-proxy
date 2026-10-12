# kubernetes-rce-nodes-proxy

Run shell commands inside **any container on a Kubernetes node** using only a
"read-only" service account token that has the permission
`get nodes/proxy`.

That permission looks harmless. It is not. With it you can open a WebSocket to
the kubelet API and `exec` into any pod on that node — as root, in most cases.

Built for the Hack The Box machine **Fireflow**, and useful as a general
red-team / detection-testing tool.

---

## TL;DR

```bash
pip install -r requirements.txt

# 1. Edit the 5 constants at the top of the script
# 2. Run a one-shot command
python3 kubernetes_rce_nodes_proxy.py id

# or drop into an interactive shell
python3 kubernetes_rce_nodes_proxy.py
```

---

## What you need

- A bearer token (a Kubernetes service account JWT) that has
  `get nodes/proxy` on the cluster.
- Network access to a node's kubelet API: `https://<node-ip>:10250`
- Python 3.8+ and the `websockets` package.

If you already have a shell inside a pod, the token is sitting right there:

```bash
cat /var/run/secrets/kubernetes.io/serviceaccount/token
```

---

## Configuration

Open the script and edit the constants at the top:

```python
NODE_IP    = "10.129.143.154"                       # the node you want to hit
NS         = "monitoring"                           # namespace of the target pod
POD        = "prometheus-prometheus-node-exporter-nmntq"
CONTAINER  = "node-exporter"
TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
```

`TOKEN_FILE` is read automatically at startup. If your token lives somewhere
else, either put it at that path or change the value.

---

## Three ways to use it

```bash
# 1. Interactive /bin/sh (default, same as passing no arguments)
python3 kubernetes_rce_nodes_proxy.py

# 2. Interactive shell, but a different binary
python3 kubernetes_rce_nodes_proxy.py -i /bin/bash

# 3. One-shot command — runs it and exits
python3 kubernetes_rce_nodes_proxy.py id
python3 kubernetes_rce_nodes_proxy.py cat /etc/shadow
python3 kubernetes_rce_nodes_proxy.py sh -c "cat /root/root.txt; hostname"
```

Anything after the script name is treated as the command and its arguments.
No argument parsing tricks, no shell wrapping — each word becomes its own
`command=` parameter in the URL.

---

## How it works, step by step

### 1. It builds a kubelet `exec` URL

```
wss://<NODE_IP>:10250/exec/<namespace>/<pod>/<container>?output=1&error=1&stdin=1&tty=1&command=id
```

That is the kubelet's own exec endpoint — the same one `kubectl exec` ends up
talking to, except we go **straight to the node** instead of through the
apiserver.

### 2. It opens a WebSocket instead of a POST

The kubelet decides which RBAC verb to check by looking at the **HTTP method**
of the request:

| HTTP method | RBAC verb checked | endpoints |
|-------------|-------------------|-----------|
| `GET`       | `get`             | `/healthz`, `/pods`, **`/exec` (WebSocket upgrade)** |
| `POST`      | `create`          | `/exec`, `/attach`, `/portforward` (SPDY) |

A WebSocket handshake is **always an HTTP `GET`**. So the kubelet authorizes
our upgrade as `get nodes/proxy` — the permission we hold — and never asks
whether we have `create`.

That is the whole trick.

The connection is made with:

- `ssl._create_unverified_context()` — kubelet certs are usually self-signed,
  so certificate verification is skipped.
- Subprotocol `v4.channel.k8s.io` — Kubernetes' standard multiplexing
  protocol for exec sessions.
- `Authorization: Bearer <token>` header.

### 3. It speaks the Kubernetes channel protocol

Kubernetes does not send plain text over this socket. Every message is:

```
[1 byte channel id][payload]
```

| channel | meaning | what the script does with it |
|---------|---------|------------------------------|
| `0`     | stdin   | writes your typed lines here |
| `1`     | stdout  | prints to your terminal |
| `2`     | stderr  | prints to your terminal |
| `3`     | status  | JSON; anything but `"Success"` is printed as an error |

The script reads messages off the socket, looks at byte `0`, and routes the
rest to the right place. When a command finishes, the kubelet sends a `ch 3`
message like `{"status":"Success"}`, and the loop ends.

### 4. It pumps your keyboard into channel 0

In interactive mode a background task reads `stdin` line by line, prefixes
each line with `\x00` (the channel 0 byte) and sends it. When your input
closes it sends a bare `\x00` — that is the Kubernetes EOF marker.

---

## Finding other pods to jump into

`/pods` is also a `GET`, so the same token lists every pod on the node:

```bash
curl -sk -H "Authorization: Bearer $TOKEN" https://<node-ip>:10250/pods | \
  python3 -c "import json,sys;[print(p['metadata']['namespace'],p['metadata']['name'],[c['name'] for c in p['spec']['containers']]) for p in json.load(sys.stdin)['items']]"
```

Copy the namespace, pod name and container name into the constants at the top
of the script and re-run.

---

## Why this is allowed (and why it matters)

The issue was disclosed by Graham Helton in
[Kubernetes Remote Code Execution via Nodes/Proxy GET Permission](https://grahamhelton.com/blog/nodes-proxy-rce)
(January 2026) and reported upstream. It was closed as *working as intended*.

Two things make it work:

1. WebSocket handshakes are `GET`, so `/exec` is authorized as `get`.
2. SPDY upgrades can be forced to `GET` too (client-go defaults to `POST`,
   but `remotecommand.NewSPDYExecutor(config, "GET", url)` overrides it).

Consequences worth knowing:

- `get nodes/proxy` effectively means **exec on every pod** of any node you
  can reach on TCP/10250. The permission appears in 69 public Helm charts.
- Talking to the kubelet directly **bypasses the apiserver**, so
  `kubectl exec`-style audit logs never see your commands.
- Going through the apiserver proxy path
  (`/api/v1/nodes/<node>/proxy/exec/...`) usually fails, because the query
  string is dropped during the upgrade. **Talk to the kubelet directly.**

### Defensive notes

- Treat `get nodes/proxy` as equivalent to full cluster exec. Audit who holds
  it, especially monitoring service accounts.
- Restrict TCP/10250 with NetworkPolicy or a host firewall.
- Look at `KubeletFineGrainedAuthz` (KEP-2862) where your version supports it.

---

## Notes and limitations

- WebSocket only. Older kubelets (before ~v1.34ish) that do not serve native
  WebSocket exec will reject the handshake — use a SPDY client for those,
  e.g. [`zwindler/nodes-proxy-rce-poc`](https://github.com/zwindler/nodes-proxy-rce-poc).
- Certificate verification is off by design. There is no CA pinning.
- The script assumes you are running it **from inside the cluster** (in-pod
  token file). Running it from outside? Point `TOKEN_FILE` at a saved token.
- Hardcoded targets are intentional — this is a PoC, not a scanner.

---

## Requirements file

```
websockets>=10.0
```

---

## References

- Graham Helton — [Nodes/Proxy GET to RCE](https://grahamhelton.com/blog/nodes-proxy-rce)
- Horizon3 — [When Read-Only Isn't: K8s nodes/proxy GET to RCE](https://horizon3.ai/attack-research/when-read-only-isnt-k8s-nodes-proxy-get-to-rce/)
- [zwindler/nodes-proxy-rce-poc](https://github.com/zwindler/nodes-proxy-rce-poc) — Go + Python PoC with SPDY fallback
- [cristhian-bot0/kubelet-pwn](https://github.com/cristhian-bot0/kubelet-pwn) — Python SPDY implementation
- Upstream issues: [kubernetes/kubernetes#78741](https://github.com/kubernetes/kubernetes/issues/78741),
  [#119640](https://github.com/kubernetes/kubernetes/issues/119640),
  [PR #134577](https://github.com/kubernetes/kubernetes/pull/134577),
  [KEP-4006](https://kep.k8s.io/4006), [KEP-2862](https://kep.k8s.io/2862)

---

## Legal

Only run this against clusters you own or have explicit written permission to
test. Unauthorised access to computer systems is illegal.
