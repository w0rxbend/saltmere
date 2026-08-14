---
title: "The Init Container Pattern: Preparing State Before the Main Container Runs"
date: 2026-08-14
track: sys-patterns
summary: "An init container is a run-to-completion setup step that finishes before your app container ever starts — wait for a dependency, run a migration, fetch a secret, fix a volume's permissions — and it is a strictly different animal from the long-lived sidecar it is often confused with."
reading_time: 5
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

Brendan Burns' *Designing Distributed Systems* groups the useful multi-container shapes into two families: single-node patterns, where containers share a machine and a lifecycle, and multi-node patterns, where they don't. The sidecar and the ambassador live in that first family. So does a quieter member that rarely gets top billing: the **init container** — a container whose entire job is to run once, to completion, *before* the application container is allowed to start.

The distinction matters because sidecars and init containers are constantly conflated, and they solve opposite problems. A sidecar runs *alongside* your app for the whole life of the pod. An init container runs *ahead* of it and then exits. One decorates a running process; the other prepares the ground the process will stand on.

## Run-to-completion, in order, before anything else

Kubernetes gives init containers three guarantees that ordinary containers don't have:

1. They run **before** any app container starts.
2. They run **sequentially**, in the order listed — each must succeed before the next begins.
3. They must **run to completion**; a failed init container is retried per the pod's `restartPolicy` until it succeeds, and the app containers never start in the meantime.

That third point is the whole value. An init container is a hard gate. If the database isn't reachable or the migration didn't apply, your app image never runs at all — you get a pod stuck in `Init:` rather than a crash-looping app that half-started against a broken world.

## What people actually use them for

The pattern shows up whenever the app needs the environment to be in a known state:

- **Wait-for-dependency** — block until a database, cache, or service DNS name resolves and accepts connections.
- **Schema migration** — run `migrate up` (or Flyway/Liquibase) once, before any replica of the app boots.
- **Fetch config or secrets** — pull material from a vault or object store into a shared `emptyDir` the app then reads.
- **Fix permissions / seed volumes** — `chown` a mounted PVC, or unpack a base dataset, so the app image can stay unprivileged and read-only.

Here is a pod that does two of these: it waits for Postgres to come up, then chowns a shared data volume, before the app container ever runs.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: reporting
spec:
  initContainers:
    - name: wait-for-db                 # gate 1: don't start until DB answers
      image: busybox:1.36
      command: ['sh', '-c',
        'until nc -z postgres 5432; do echo waiting for db; sleep 2; done']
    - name: fix-perms                    # gate 2: hand the app a writable dir
      image: busybox:1.36
      command: ['sh', '-c', 'chown -R 1000:1000 /data']
      volumeMounts:
        - { name: work, mountPath: /data }
  containers:
    - name: app                          # only starts after both gates pass
      image: reporting:4.2
      securityContext: { runAsUser: 1000 }
      volumeMounts:
        - { name: work, mountPath: /data }
  volumes:
    - name: work
      emptyDir: {}
```

Watch the ordering play out:

```sh
kubectl apply -f reporting.yaml
kubectl get pod reporting -w
# NAME        READY   STATUS            RESTARTS
# reporting   0/1     Init:0/2          0        # wait-for-db running
# reporting   0/1     Init:1/2          0        # fix-perms running
# reporting   0/1     PodInitializing   0        # both done, app pulling
# reporting   1/1     Running           0
kubectl logs reporting -c wait-for-db            # -c selects the init container
```

## Init containers vs. native sidecars

For years people abused init containers to run *long-lived* helpers — a proxy or log shipper — which broke, because a never-exiting init container blocks the app forever. Kubernetes fixed this by making the sidecar a first-class kind of init container: an entry in `initContainers` with `restartPolicy: Always`. Such a container **starts** in init order but is considered ready once it is up, so the sequence proceeds — and it keeps running for the life of the pod, restarting independently if it dies.

The `SidecarContainers` feature went beta and on-by-default in **Kubernetes 1.29**, and reached **stable (GA) in Kubernetes 1.33**. The rule of thumb: no `restartPolicy` on an init container means "run once, then get out of the way"; `restartPolicy: Always` means "start early, then stay." Pick by lifecycle, not by habit.

**Try next:** Take a pod that currently races its database and add a `wait-for-db` init container, then `kubectl get pod -w` and confirm it sits in `Init:0/1` until the dependency is live.
