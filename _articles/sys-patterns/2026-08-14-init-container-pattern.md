---
title: "The Init Container Pattern: Preparing State Before the Main Container Runs"
date: 2026-08-14
track: sys-patterns
summary: "An init container is a run-to-completion setup step that finishes before the application container starts — waiting for a dependency, running a migration, fetching a secret, correcting a volume's ownership — and it is distinct from the long-lived sidecar with which it is frequently confused."
reading_time: 6
tags: [init-containers, sidecar, kubernetes, pods, patterns, burns]
sources:
  - title: "Kubernetes: Init Containers"
    url: "https://kubernetes.io/docs/concepts/workloads/pods/init-containers/"
  - title: "Kubernetes: Sidecar Containers"
    url: "https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/"
  - title: "Brendan Burns, Designing Distributed Systems (2nd ed.) — Single-Node Patterns"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/"
  - title: "Configure Native Sidecar Containers with restartPolicy for Kubernetes 1.29+ — OneUptime"
    url: "https://oneuptime.com/blog/post/2026-02-09-native-sidecar-restart-policy/view"
---

**Gist.** An application container that starts before its environment is ready fails in the worst available way: it half-initialises against a broken world and then crash-loops, producing restart noise instead of a diagnosis. The init container converts that race into a **gate** — a container that must run to completion, in declared order, before any application container in the pod is started. The cost is serialised startup: every init container's runtime is added to the pod's time-to-ready, and a gate that never succeeds holds the pod in `Init:` indefinitely rather than surfacing an application-level error.

Brendan Burns' *Designing Distributed Systems* groups the useful multi-container shapes into two families: single-node patterns, in which containers share a machine and a lifecycle, and multi-node patterns, in which they do not. The sidecar, the ambassador and the adapter are the single-node patterns the book names. The **init container** shares that family's defining property — one pod, one lifecycle — while inverting the timing: its entire task is to run once, to completion, before the application container is permitted to start.

The distinction is worth stating precisely, because sidecars and init containers are routinely conflated while solving opposite problems. A sidecar runs *alongside* the application for the whole life of the pod. An init container runs *ahead* of it and then exits. One decorates a running process; the other prepares the ground that process will stand on.

## The three guarantees

Kubernetes gives init containers three properties that ordinary containers in a pod do not have:

1. They run **before** any application container starts.
2. They run **sequentially**, in the order listed in `initContainers` — each must terminate successfully before the next begins.
3. They must **run to completion**. The handling of a failure depends on the pod's `restartPolicy`: under `Always` (and `OnFailure`) the kubelet restarts the init container until it exits zero; under `Never` the pod is marked failed. In neither case are the application containers started.

The third property carries the value. **The init container is a hard gate, and its exit status is the gate condition.** If the database is unreachable or a migration has not applied, the application image is never executed: the pod is observable in `Init:` state rather than as a crash-looping application whose logs mix startup failure with partial work.

The ordering also gives an invariant that ordinary containers cannot express: **any filesystem or network state established by init container *i* is visible to init container *i+1* and to every application container**, because the later containers do not exist until the earlier one has exited zero. A shared `emptyDir` volume is the usual carrier for that state.

Because init containers run at distinct times rather than concurrently with the application, resource accounting differs from ordinary containers: the effective request for a resource is the larger of the highest init-container request and the sum of the application containers' requests, since the two sets never contend for the node at the same moment.

## Established uses

The pattern applies wherever the application requires the environment to be in a known state before its first instruction runs:

- **Wait-for-dependency** — block until a database, cache, or service DNS name resolves and accepts a connection.
- **Schema migration** — run `migrate up`, Flyway, or Liquibase once, before any replica of the application boots.
- **Fetch configuration or secrets** — pull material from a secret store or object store into a shared `emptyDir` that the application then reads, keeping credential-fetching tooling out of the application image.
- **Correct permissions or seed volumes** — change ownership on a mounted persistent volume, or unpack a base dataset, so the application image can remain unprivileged with a read-only root filesystem.

The following pod performs two of these: it waits for PostgreSQL to accept connections, then adjusts ownership of a shared data volume, before the application container runs.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: reporting
spec:
  initContainers:
    - name: wait-for-db                  # gate 1: no start until the DB answers
      image: busybox:1.36
      command: ['sh', '-c',
        'until nc -z postgres 5432; do echo waiting for db; sleep 2; done']
    - name: fix-perms                    # gate 2: hand the app a writable dir
      image: busybox:1.36
      command: ['sh', '-c', 'chown -R 1000:1000 /data']
      securityContext: { runAsUser: 0 }  # only this container needs root
      volumeMounts:
        - { name: work, mountPath: /data }
  containers:
    - name: app                          # starts only after both gates pass
      image: reporting:4.2
      securityContext: { runAsUser: 1000, readOnlyRootFilesystem: true }
      volumeMounts:
        - { name: work, mountPath: /data }
  volumes:
    - name: work
      emptyDir: {}
```

Two details are load-bearing. **The `chown` container is the only one granted UID 0**, which is what makes the privilege asymmetry pay: the long-lived process runs unprivileged over a directory prepared by a short-lived privileged one. And **`wait-for-db` loops rather than failing fast** — an exiting-nonzero probe would also work, but it delegates the retry interval to the pod's restart backoff instead of the loop's `sleep 2`.

The status transitions are directly observable:

```sh
kubectl apply -f reporting.yaml
kubectl get pod reporting -w
# NAME        READY   STATUS            RESTARTS
# reporting   0/1     Init:0/2          0        # wait-for-db running
# reporting   0/1     Init:1/2          0        # fix-perms running
# reporting   0/1     PodInitializing   0        # both done, app image pulling
# reporting   1/1     Running           0
kubectl logs reporting -c wait-for-db            # -c selects the init container
```

`Init:N/M` reports that *N* of *M* init containers have completed; `PodInitializing` marks the interval after the last init container exits and before the application containers are running. **Logs of an init container remain retrievable by name after it exits**, which is the primary diagnostic when a pod stalls.

## Init containers and native sidecars

Init containers were for a period pressed into running *long-lived* helpers such as a proxy or a log shipper. That use is incorrect under the second guarantee above: a container that never exits blocks the pod's initialisation permanently. Kubernetes addressed this by making the sidecar a form of init container — an entry in `initContainers` carrying `restartPolicy: Always`. Such a container **starts in init order but is treated as satisfied once it is running rather than once it has exited**, so the sequence advances past it; it then continues for the life of the pod and restarts independently if it terminates.

The `SidecarContainers` feature went beta and on-by-default in **Kubernetes 1.29**, and reached **stable (general availability) in Kubernetes 1.33**. The selection rule follows from the lifecycle rather than from convention: an init container with no `restartPolicy` means run once and exit; `restartPolicy: Always` means start early and remain.

## Pitfalls

- **A long-lived process in `initContainers` without `restartPolicy: Always` hangs the pod forever.** The pod sits in `Init:N/M` with no application logs, because completion — not readiness — is the advancement condition for a plain init container.
- **A migration init container attached to a multi-replica Deployment runs once per replica, concurrently.** Every pod executes its own copy of the gate, so the migration tool must itself be safe under concurrent invocation; the pattern provides ordering within a pod, not across pods.
- **A wait loop with no upper bound converts a dependency outage into an indefinite `Init:` stall.** The pod is neither ready nor failing, so alerting keyed on `CrashLoopBackOff` or restart counts never fires.
- **State written outside a shared volume does not survive the init container.** Files placed on the init container's own writable layer are discarded when it exits; only paths under a volume mounted into both containers are visible to the application.
- **Init containers are added to time-to-ready on every restart and reschedule**, not only on first deployment, so a slow gate multiplies across rolling updates and node evictions.
- **Ordinary probes do not apply to a plain init container**, which supports neither `readinessProbe` nor `livenessProbe`; only an init container with `restartPolicy: Always` accepts probes. A plain init container has no readiness gate of its own, so a process that starts but never becomes functional is indistinguishable from useful work until it exits.
