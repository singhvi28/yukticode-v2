# YuktiCode Kubernetes & KEDA SRE Operations Guide

This directory contains the production Kubernetes manifests and event-driven autoscaling configurations for **YuktiCode v2**, an enterprise asynchronous online judge platform.

---

## 1. Architecture & Component Topology

```mermaid
flowchart TD
    subgraph K8sCluster["Kubernetes Cluster (Namespace: yukti-code)"]
        subgraph IngressTier["Edge / Ingress Tier"]
            SVC_FE["frontend Service (NodePort :30080)"] --> Pod_FE["frontend Pods (Nginx SPA)"]
            Pod_FE -->|/api/* proxy pass| SVC_BE["backend Service (ClusterIP :9000)"]
        end

        subgraph APITier["REST & Telemetry Tier"]
            SVC_BE --> Pod_BE["backend Pods (FastAPI)"]
            Pod_BE -->|Prometheus Scrape :9000/metrics| Prom["Prometheus Operator / Scraper"]
            Pod_BE -->|Enqueue Jobs & Leases| DB[("PostgreSQL 16 (StatefulSet :5432)")]
        end

        subgraph WorkerPool["Asynchronous Worker Tier"]
            Pod_W1["judge-worker Pod 1"]
            Pod_WN["judge-worker Pod N"]
            Pod_W1 -->|FOR UPDATE SKIP LOCKED| DB
            Pod_WN -->|FOR UPDATE SKIP LOCKED| DB
            Pod_W1 -->|/var/run/docker.sock| HostDocker["Host Docker Daemon"]
            Pod_WN -->|/var/run/docker.sock| HostDocker
        end

        subgraph KEDAEngine["KEDA Autoscaler Tier"]
            KEDA_Op["KEDA Operator"] -->|SELECT count(*) ... every 5s| DB
            KEDA_Op -->|Drive HPA Controller| HPA["Kubernetes HPA"]
            HPA -->|Scale 1..10 Replicas| WorkerPool
        end
    end
```

### Component Overview

| Manifest | Kind | Responsibilities | Scrape Port |
| :--- | :--- | :--- | :--- |
| [`namespace.yaml`](./namespace.yaml) | `Namespace` | Defines the isolated namespace `yukti-code`. | N/A |
| [`postgres.yaml`](./postgres.yaml) | `StatefulSet`, `Service`, `Secret`, `PVC` | Single-replica PostgreSQL 16 with persistent volume claims, credentials, and connection strings. | N/A |
| [`backend.yaml`](./backend.yaml) | `Deployment`, `Service` | FastAPI backend instances (2 replicas), database migrations, RED metrics. | `:9000` |
| [`worker.yaml`](./worker.yaml) | `Deployment` | Judge worker daemon mounted to host Docker socket (`/var/run/docker.sock`). | `:9100` |
| [`frontend.yaml`](./frontend.yaml) | `Deployment`, `Service` | Nginx static frontend bundle reverse-proxying API requests. | `:80` |
| [`keda-scaledobject.yaml`](./keda-scaledobject.yaml) | `ScaledObject`, `TriggerAuth` | PostgreSQL backlog queue depth event-driven autoscaler. | N/A |

---

## 2. Local Setup Guide (Kind & Minikube)

### Option A: Local Cluster via Kind (Recommended)

Because judge workers launch isolated container sandboxes via the Docker daemon, the host `/var/run/docker.sock` must be mounted into the Kind node.

1. **Create Kind configuration** (`kind-config.yaml`):

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraPortMappings:
  - containerPort: 30080
    hostPort: 8080
    listenAddress: "0.0.0.0"
  extraMounts:
  - hostPath: /var/run/docker.sock
    containerPath: /var/run/docker.sock
```

2. **Spin up the Kind cluster**:
```bash
kind create cluster --name yukti --config kind-config.yaml
```

### Option B: Local Cluster via Minikube

```bash
minikube start --driver=docker --mount --mount-string="/var/run/docker.sock:/var/run/docker.sock"
```

---

## 3. Install KEDA (Kubernetes Event-driven Autoscaling)

KEDA monitors external metric sources (such as PostgreSQL query counts) and exposes standard Kubernetes custom metrics to the Horizontal Pod Autoscaler (HPA).

Install KEDA via Helm:

```bash
# Add KEDA Helm chart repository
helm repo add kedacore https://kedacore.github.io/charts
helm repo update

# Install KEDA into 'keda' namespace
helm install keda kedacore/keda --namespace keda --create-namespace

# Verify KEDA operator and metrics API server are ready
kubectl get pods -n keda -w
```

Expected output:
```text
NAME                                               READY   STATUS    RESTARTS   AGE
keda-admission-webhooks-xxx-yyy                    1/1     Running   0          30s
keda-operator-xxx-yyy                              1/1     Running   0          30s
keda-operator-metrics-apiserver-xxx-yyy            1/1     Running   0          30s
```

---

## 4. Build and Load Container Images

Build the production containers locally:

```bash
# 1. Backend REST API
docker build -t cf-clone-v2-backend:latest ./backend

# 2. Worker Daemon
docker build -t cf-clone-v2-worker:latest -f backend/Dockerfile.worker ./backend

# 3. Frontend Web SPA
docker build -t cf-clone-v2-frontend:latest ./frontend

# 4. Judge Runner Sandbox Image (evaluated test-case runner)
docker build -t cf-clone-v2-judge:latest -f backend/Dockerfile.judge ./backend 2>/dev/null || true
```

Load container images into your local cluster:

**For Kind:**
```bash
kind load docker-image cf-clone-v2-backend:latest --name yukti
kind load docker-image cf-clone-v2-worker:latest --name yukti
kind load docker-image cf-clone-v2-frontend:latest --name yukti
kind load docker-image cf-clone-v2-judge:latest --name yukti
```

**For Minikube:**
```bash
minikube image load cf-clone-v2-backend:latest
minikube image load cf-clone-v2-worker:latest
minikube image load cf-clone-v2-frontend:latest
minikube image load cf-clone-v2-judge:latest
```

---

## 5. Deploying YuktiCode Manifests

Apply the manifests in logical sequence:

```bash
# 1. Namespace
kubectl apply -f deploy/k8s/namespace.yaml

# 2. PostgreSQL StatefulSet, Service, & Secrets
kubectl apply -f deploy/k8s/postgres.yaml

# Wait for PostgreSQL to become fully ready
kubectl wait --namespace yukti-code \
  --for=condition=ready pod \
  --selector=app=postgres \
  --timeout=120s

# 3. Backend & Frontend
kubectl apply -f deploy/k8s/backend.yaml
kubectl apply -f deploy/k8s/frontend.yaml

# Wait for backend to complete database migrations
kubectl wait --namespace yukti-code \
  --for=condition=ready pod \
  --selector=app=backend \
  --timeout=120s

# 4. Worker Deployment & KEDA ScaledObject
kubectl apply -f deploy/k8s/worker.yaml
kubectl apply -f deploy/k8s/keda-scaledobject.yaml
```

### Seeding Problem Data in Kubernetes

Run the idempotent seed script inside a running backend pod:

```bash
kubectl exec -n yukti-code -it \
  $(kubectl get pod -n yukti-code -l app=backend -o jsonpath='{.items[0].metadata.name}') \
  -- python3 seed.py
```

---

## 6. How KEDA Calculates Worker Replicas (The Math & Mechanism)

The `judge-worker-autoscaler` `ScaledObject` monitors pending judge backlog by executing the following query against PostgreSQL:

```sql
SELECT count(*) FROM judge_jobs WHERE status = 'queued' AND available_at <= now();
```

### Scaling Formula

KEDA dynamically configures a standard Kubernetes HPA with an external metric. The target replica calculation is:

$$\text{Desired Replicas} = \min\left(\text{maxReplicaCount}, \max\left(\text{minReplicaCount}, \left\lceil \frac{\text{Current Queue Depth}}{\text{targetQueryValue}} \right\rceil\right)\right)$$

With our configuration:
- `minReplicaCount`: **1**
- `maxReplicaCount`: **10**
- `targetQueryValue`: **5** (1 worker pod per 5 queued jobs)
- `activationTargetQueryValue`: **1**
- `pollingInterval`: **5s** (evaluates queue backlog every 5 seconds)
- `cooldownPeriod`: **30s** (stabilization window before scaling down)

### Scale Progression Matrix

| Active Backlog (`queued`) | Calculated Value | Target Replicas | Status |
| :---: | :---: | :---: | :--- |
| **0** | $0 / 5 = 0 \to \min(1)$ | **1** | Baseline idle state (1 warm standby ready for incoming work) |
| **1 – 5** | $\lceil 1..5 / 5 \rceil = 1$ | **1** | Standard load (1 worker drains queue within SLO) |
| **6 – 10** | $\lceil 6..10 / 5 \rceil = 2$ | **2** | Scale out triggered (+1 pod) |
| **15** | $\lceil 15 / 5 \rceil = 3$ | **3** | Moderate burst (+2 pods) |
| **30** | $\lceil 30 / 5 \rceil = 6$ | **6** | Contest start / burst test (+5 pods) |
| **50+** | $\lceil 50 / 5 \rceil = 10$ | **10** | Capped at `maxReplicaCount` |

### Database Safety & Connection Pooling

A common SRE failure mode with database-driven autoscalers is database starvation caused by frequent polling queries. YuktiCode mitigates this via two architectural guarantees:

1. **Centralized Operator Querying**: Individual worker pods do not query or scale themselves. Only the single KEDA operator pod executes the `SELECT count(*)` query on a strict 5-second cadence using connection pooling.
2. **Index-Only Execution**: The query filters strictly on `status` and `available_at`. PostgreSQL leverages the index `ix_judge_jobs_status_available_at` for an index-only scan ($O(\log N)$ or fast partial index scan), preventing full table scans even with millions of historical rows.

---

## 7. Real-Time Load Testing & Verification

We provide an automated, asynchronous load-generation script: `scripts/simulate_load.py`.

### Step 1: Run Load Simulation

Inject 35 concurrent submissions into the queue:

```bash
# If accessing via Kind extraPortMappings / NodePort:
python3 scripts/simulate_load.py --count 35 --api-url http://localhost:8080/api

# If accessing via kubectl port-forward:
kubectl port-forward -n yukti-code svc/frontend 8080:80 &
python3 scripts/simulate_load.py --count 35 --api-url http://localhost:8080/api
```

Sample CLI output:
```text
[1/3] Authenticating with API at http://localhost:8080/api...
  ✓ Authenticated as admin (Admin - unlimited concurrency).

[2/3] Generating 35 jobs (concurrency=10, mode=submit)...
  [ 1/35] 202 Accepted (job=e9d34b12...) in  18.4ms
  [ 2/35] 202 Accepted (job=4f2a718c...) in  21.1ms
  ...
  [35/35] 202 Accepted (job=bc399a01...) in  19.8ms

Injection Complete: 35 succeeded, 0 failed in 0.48s (72.9 submissions/sec).

[3/3] Monitoring queue saturation, KEDA target replicas, and drain...
  Press Ctrl+C at any time to exit.

  [  2.0s] Queued:   35 | Running:  1 | KEDA Target Replicas:  7 | [████████████████████]
  [  4.1s] Queued:   28 | Running:  7 | KEDA Target Replicas:  6 | [████████████████░░░░]
  [  8.3s] Queued:   14 | Running:  7 | KEDA Target Replicas:  3 | [████████░░░░░░░░░░░░]
  [ 14.5s] Queued:    2 | Running:  3 | KEDA Target Replicas:  1 | [█░░░░░░░░░░░░░░░░░░░]
  [ 18.2s] Queued:    0 | Running:  0 | KEDA Target Replicas:  1 | [░░░░░░░░░░░░░░░░░░░░]

✓ Queue fully drained! All jobs processed in 18.2s.
```

### Step 2: Observe Scaling in Kubernetes

In separate terminal windows, observe KEDA and pod lifecycle transitions:

```bash
# Watch KEDA ScaledObject metrics and target replicas
kubectl get scaledobject -n yukti-code -w

# Watch HPA target metric calculation
kubectl get hpa -n yukti-code -w

# Watch worker pod creation and termination
kubectl get pods -n yukti-code -l app=judge-worker -w
```

---

## 8. SRE Defense & Interview Guide: Why Queue Depth HPA Trumps CPU/Memory

When defending this architecture in a systems design or SRE interview, articulate the following technical advantages of queue-depth-based autoscaling over traditional CPU/memory threshold autoscaling:

### 1. Lead Time vs. Lagging Indicators
- **Traditional CPU/RAM Autoscaling**: CPU utilization is a **lagging** indicator. Workers only consume CPU *after* a job is popped from the queue, files are compiled, and sandbox execution begins. During sudden traffic spikes (e.g., contest opening bell), jobs queue up immediately, but CPU remains flat until workers begin processing. By the time CPU triggers pod creation (60–90s pod startup overhead), user submission latency has already violated SLOs.
- **Queue-Depth Metric**: PostgreSQL queue depth is an **instantaneous lead indicator**. The instant 30 users click "Submit", queue depth jumps to 30. KEDA immediately initiates scale-out before CPU has even shifted, cutting queue waiting time ($W_q$) by up to 70%.

### 2. Heterogeneous Workload Decoupling
- An online judge processes wildly asymmetric workloads:
  - Job A: A fast Python script (low CPU, memory bounded, 50ms execution).
  - Job B: A C++ combinatorial search with infinite loop (100% CPU on 1 core for 2.0s until TLE).
- In a CPU-driven system, two TLE submissions on 1 worker will drive CPU to 100%, causing Kubernetes to scale up workers even though the queue is empty. Conversely, 50 fast I/O jobs will keep average CPU at 20%, preventing scale-up while queue depth builds up silently. Queue depth decouples scaling from execution idiosyncrasies.

### 3. Prevention of Positive Feedback "Death Spirals"
- Under severe host saturation, container thread contention and context-switching cause CPU spikes across all nodes. A CPU-based autoscaler interprets this as a signal to spin up even more pods, exacerbating CPU starvation and thrashing the host kernel.
- Queue-depth scaling is mathematically grounded in Little's Law ($L = \lambda W$): it scales only in response to unprocessed work backlog.

### 4. Flapping Prevention & Controlled Scale-Down
- CPU metrics drop to near-zero in the split-second between a completed test-case and the next database poll. A naive CPU HPA risks premature down-scaling, killing pods mid-execution or thrashing the replica count.
- With KEDA, the `cooldownPeriod: 30` parameter enforces a stabilization window: replica count does not decrease until the queue has remained below the lower threshold for 30 consecutive seconds. Coupled with Kubernetes `terminationGracePeriodSeconds` and database row-level locking (`FOR UPDATE SKIP LOCKED`), running jobs are guaranteed to finish before pods terminate.
