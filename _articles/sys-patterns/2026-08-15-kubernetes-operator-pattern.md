---
title: "The Operator Pattern: Reconciliation Loops as an Architecture"
date: 2026-08-15
track: sys-patterns
summary: "An operator is two things: a CRD that lets you declare 'a PostgresCluster named orders, 3 replicas' as a Kubernetes object, and a controller that endlessly reconciles the world toward that declaration. The architecture lesson generalizes beyond Kubernetes: level-based reconciliation ('converge on current state') beats edge-triggered event handling ('react to each change') because it self-heals from missed events. Here is the reconcile contract — idempotence, status subresource, owner references — and when not to build one."
reading_time: 5
tags: [kubernetes, operator, crd, reconciliation, controller, kubebuilder]
sources:
  - title: "Operator pattern — Kubernetes Documentation"
    url: "https://kubernetes.io/docs/concepts/extend-kubernetes/operator/"
  - title: "Introducing Operators: Putting Operational Knowledge into Software — CoreOS (Red Hat Blog, 2016)"
    url: "https://www.redhat.com/en/blog/introducing-operators-putting-operational-knowledge-into-software"
  - title: "The Kubebuilder Book — kubernetes-sigs"
    url: "https://book.kubebuilder.io/"
  - title: "Controllers — Kubernetes Documentation"
    url: "https://kubernetes.io/docs/concepts/architecture/controller/"
  - title: "Garbage Collection (owner references) — Kubernetes Documentation"
    url: "https://kubernetes.io/docs/concepts/architecture/garbage-collection/"
---

CoreOS coined "operator" in a 2016 blog post with a precise definition: *"an application-specific controller that extends the Kubernetes API to create, configure, and manage instances of complex stateful applications"* — operational knowledge (how to upgrade etcd, how to reshard, how to restore a backup) encoded in software that runs the runbook for you. A decade on, the pattern is the standard way to run databases, message brokers, and cert management on Kubernetes. But the durable idea is smaller than "automate ops": it is the **reconciliation loop** as a design discipline.

## The two halves

A **CustomResourceDefinition (CRD)** teaches the API server a new type. After applying one, `kubectl get postgresclusters` works, and users declare intent as data:

```yaml
apiVersion: db.example.com/v1alpha1
kind: PostgresCluster
metadata: { name: orders }
spec:     { replicas: 3, version: "17.4", backupSchedule: "0 3 * * *" }
```

A CRD alone is inert — a schema in a database. The **custom controller** supplies behavior: it watches those objects and drives reality toward them. Every controller implements the same contract: compare **desired state** (`spec`, written by users) with **observed state** (what actually exists), and act to close the gap. `spec` is the user's; **`status` is the controller's** — which is why the **status subresource** exposes `status` at a separate API endpoint with separate RBAC, so a controller updating `status.readyReplicas` can never race a user editing `spec`, and vice versa.

## Level-based, not edge-triggered

The design decision that makes controllers robust: reconciliation is **level-based** (respond to the current state, however you got there), not **edge-triggered** (respond to each transition). An edge-triggered controller handles "replicas went 3→5" by creating two pods; if it was down during the event, or two events coalesced, it is now permanently wrong. A level-based controller handles "desired is 5" by listing pods, counting 3, and creating 2 — **the event is only a hint about *when* to look, never *what* happened.** Missed events, restarts, and duplicate deliveries all converge to the same answer on the next pass, which is also why controllers can resync periodically from a cache rather than requiring a lossless event stream.

| | Edge-triggered | Level-based |
|---|---|---|
| **Reacts to** | Each state *transition* | Current state vs desired |
| **Missed event** | Permanently diverged | Healed on next reconcile |
| **Duplicate event** | Double-applied | Harmless (no-op) |
| **Requires** | Reliable, ordered delivery | Idempotent reconcile |

The corollary is that **reconcile must be idempotent**: running it twice against an already-correct world does nothing. In controller-runtime (the library under kubebuilder, currently the v4.x line — v4.9 as of mid-2026), that shape is explicit:

```go
func (r *PostgresReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var pg dbv1.PostgresCluster
    if err := r.Get(ctx, req.NamespacedName, &pg); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err) // deleted: owner refs GC children
    }

    desired := statefulSetFor(&pg)                        // pure function of spec
    ctrl.SetControllerReference(&pg, desired, r.Scheme)   // owner ref -> garbage collection

    var current appsv1.StatefulSet
    err := r.Get(ctx, client.ObjectKeyFromObject(desired), &current)
    switch {
    case apierrors.IsNotFound(err):
        if err := r.Create(ctx, desired); err != nil { return ctrl.Result{}, err }
    case err == nil && !specEqual(&current, desired):
        if err := r.Update(ctx, desired); err != nil { return ctrl.Result{}, err }
    }

    pg.Status.ReadyReplicas = current.Status.ReadyReplicas
    if err := r.Status().Update(ctx, &pg); err != nil { return ctrl.Result{}, err }
    return ctrl.Result{RequeueAfter: 5 * time.Minute}, nil // belt-and-suspenders resync
}
```

Errors requeue with exponential backoff; there is no "compensating" path, because re-running *is* the recovery strategy.

## Owner references and cleanup

`SetControllerReference` above is deletion handled declaratively. Every child object (StatefulSet, Service, ConfigMap) carries an **ownerReference** to its PostgresCluster; delete the parent and the Kubernetes **garbage collector** cascades to the children — the controller writes no teardown code for in-cluster resources. Only external state (a cloud bucket for backups, DNS records) needs explicit cleanup, done with a **finalizer**: a marker that blocks deletion until the controller has released the external resource and removed the marker.

Scaffolding all of this is what kubebuilder is for:

```bash
kubebuilder init --domain example.com --repo example.com/postgres-operator
kubebuilder create api --group db --version v1alpha1 --kind PostgresCluster
# edit api/v1alpha1/postgrescluster_types.go and internal/controller/..., then:
make manifests install run
```

## When not to build one

The pattern has real costs — a Go codebase, CRD versioning/conversion as your schema evolves, RBAC surface, and a controller that is itself a production service to page on. Skip it when:

- **Helm/Kustomize already suffices.** If "install and upgrade" is the whole job and there are no runtime decisions, templating is cheaper. An operator earns its keep on *day-2* operations: failover, resharding, coordinated upgrades, backup/restore.
- **One exists.** CloudNativePG, Strimzi, cert-manager and the rest of OperatorHub encode years of failure modes you haven't met yet.
- **There is no reconcilable state.** Batch jobs and one-shot workflows have no "desired state to continuously converge on"; a Job or a pipeline fits better.
- **Your team won't operate the operator.** A buggy controller with cluster-wide RBAC can delete at machine speed.

The transferable lesson survives outside Kubernetes: any automation shaped as *"periodically compare desired vs observed, apply an idempotent diff"* — Terraform runs, fleet config, DNS sync — inherits the same self-healing properties, and any automation shaped as "react to each event exactly once" inherits the same fragility.

**Try next:** scaffold the PostgresCluster API above with kubebuilder, run it against a kind cluster, then `kubectl delete` the child StatefulSet mid-reconcile and watch the level-based loop recreate it without any code having handled that "event."
