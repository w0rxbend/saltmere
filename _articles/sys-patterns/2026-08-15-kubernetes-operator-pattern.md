---
title: "The Operator Pattern: Reconciliation Loops as an Architecture"
date: 2026-08-15
track: sys-patterns
summary: "An operator is two parts: a CustomResourceDefinition that makes 'a PostgresCluster named orders, 3 replicas' a first-class Kubernetes object, and a controller that repeatedly reconciles the cluster toward that declaration. The architectural claim generalizes beyond Kubernetes: level-based reconciliation, which converges on current state, tolerates missed and duplicated events that break edge-triggered event handling. This article sets out the reconcile contract — idempotence, the status subresource, owner references, finalizers — and the conditions under which the pattern does not pay."
reading_time: 6
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

**Gist.** Operating a stateful application on Kubernetes requires decisions — failover, resharding, restore — that no static manifest can express, and that fail whenever a human runbook is executed late or partially. The operator pattern encodes that knowledge as a **controller that continuously compares a declared desired state against observed state and applies the difference**, so that recovery and normal operation are the same code path. The cost is that every reconcile must be **idempotent** and that the operator itself becomes a production service holding wide privileges over the cluster it manages.

CoreOS introduced the term in a 2016 blog post with a specific definition: *"an application-specific controller that extends the Kubernetes API to create, configure, and manage instances of complex stateful applications"*. The durable idea is narrower than "automate operations": it is the **reconciliation loop** as a design discipline.

## The two halves

A **CustomResourceDefinition (CRD)** teaches the API server a new type. Once one is applied, `kubectl get postgresclusters` resolves, and intent is declared as data:

```yaml
apiVersion: db.example.com/v1alpha1
kind: PostgresCluster
metadata: { name: orders }
spec:     { replicas: 3, version: "17.4", backupSchedule: "0 3 * * *" }
```

A CRD alone is inert: it is a schema plus storage. The **custom controller** supplies behaviour, watching objects of that kind and driving the cluster toward them. The contract is uniform across controllers — compare **desired state** (`spec`, written by clients) with **observed state** (what exists), and act to close the gap.

The split of ownership is enforced structurally. `spec` belongs to the client; **`status` belongs to the controller**. The **status subresource** exposes `status` at a separate API endpoint with its own role-based access control (RBAC) rules; a request to the main endpoint ignores changes to `status`, and a request to `/status` ignores changes to everything else. Optimistic concurrency still applies to the object as a whole, so a controller write can still be rejected with a conflict and retried.

## Level-based, not edge-triggered

The property that makes controllers robust is that reconciliation is **level-based** — it responds to the current state, irrespective of the path taken to reach it — rather than **edge-triggered**, responding to each transition.

An edge-triggered controller handles the event "replicas went 3 → 5" by creating two pods. If the controller was down when the event occurred, or if two events coalesced into one delivery, the cluster is permanently wrong and nothing later corrects it: the information needed to detect the divergence was in the event, and the event is gone. A level-based controller handles "desired is 5" by listing pods, counting 3, and creating 2. **The event is a hint about *when* to look, never a statement of *what* happened.**

The invariant is therefore: *after a successful reconcile, observed state matches desired state, regardless of how many notifications were lost, reordered or duplicated beforehand.* Because the loop reads current state rather than replaying a log, it can be driven by a periodic resync from a local cache instead of a lossless ordered event stream.

| | Edge-triggered | Level-based |
|---|---|---|
| **Reacts to** | Each state *transition* | Current state vs desired |
| **Missed event** | Permanently diverged | Healed on next reconcile |
| **Duplicate event** | Double-applied | Harmless (no-op) |
| **Requires** | Reliable, ordered delivery | Idempotent reconcile |

The corollary is that **reconcile must be idempotent**: a second run against an already-correct cluster performs no writes. In controller-runtime, the library beneath kubebuilder, that shape is explicit in the signature:

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
        desired.ResourceVersion = current.ResourceVersion    // required for optimistic concurrency
        if err := r.Update(ctx, desired); err != nil { return ctrl.Result{}, err }
    }

    pg.Status.ReadyReplicas = current.Status.ReadyReplicas
    if err := r.Status().Update(ctx, &pg); err != nil { return ctrl.Result{}, err }
    return ctrl.Result{RequeueAfter: 5 * time.Minute}, nil // periodic resync
}
```

Two details carry the design. `statefulSetFor` is a **pure function of `spec`**, so the desired object is recomputed identically on every pass and never accumulates drift from earlier partial runs. A returned error requeues the request with exponential backoff; there is no compensating or rollback path, because **re-running the same function is the recovery strategy**. A partially applied reconcile — child created, `status` write failed — leaves the cluster in a state the next pass reads and completes.

## Owner references and cleanup

`SetControllerReference` makes deletion declarative. Each child object — StatefulSet, Service, ConfigMap — carries an **ownerReference** naming its PostgresCluster. Deleting the parent causes the Kubernetes **garbage collector** to cascade to the children, so the controller contains no teardown code for in-cluster resources; the deletion path is data, not logic, and cannot diverge from the creation path.

State outside the cluster — an object-storage bucket holding backups, a DNS record — has no owner reference and is not collected. Releasing it requires a **finalizer**: a string recorded on the object that causes the API server to keep the object in a terminating state rather than removing it. The controller observes the deletion timestamp, releases the external resource, removes its finalizer, and only then does the object disappear. The ordering is the point: **the object outlives the external resource it represents, so a controller restart mid-deletion re-observes the pending finalizer and retries.**

Scaffolding for the CRD, the controller skeleton and the RBAC manifests is generated by kubebuilder:

```bash
kubebuilder init --domain example.com --repo example.com/postgres-operator
kubebuilder create api --group db --version v1alpha1 --kind PostgresCluster
# edit api/v1alpha1/postgrescluster_types.go and internal/controller/..., then:
make manifests install run
```

## When the pattern does not pay

The costs are a Go codebase, CRD versioning and conversion as the schema evolves, an RBAC surface, and a controller that is itself a production service requiring on-call coverage. The pattern is a poor fit under these conditions:

- **Templating already suffices.** Where installation and upgrade are the entire job and no runtime decisions are made, Helm or Kustomize is cheaper. An operator earns its cost on day-2 operations: failover, resharding, coordinated upgrades, backup and restore.
- **An operator already exists.** CloudNativePG, Strimzi, cert-manager and the wider OperatorHub catalogue encode failure modes a new implementation has not yet encountered.
- **There is no reconcilable state.** Batch jobs and one-shot workflows have no continuously held desired state to converge on; a Job or a pipeline expresses them directly.
- **The team will not operate the operator.** A controller with cluster-wide delete permission acts at machine speed, and a reconcile bug applies uniformly to every managed object at once.

The lesson transfers outside Kubernetes. Any automation shaped as *periodically compare desired against observed, then apply an idempotent difference* — Terraform runs, fleet configuration, DNS synchronisation — inherits the same self-healing property, and any automation shaped as *react to each event exactly once* inherits the same fragility.

## Pitfalls

- **A non-idempotent reconcile duplicates resources.** If the loop creates without first reading current state, a requeue after a transient API error produces a second child object, since requeue is the normal error path rather than an exceptional one.
- **Writing to `spec` from the controller creates a fight loop.** The controller's write triggers a watch event, which triggers another reconcile, which writes again; the object churns and the API server sees unbounded update traffic. Controller output belongs in `status`.
- **A desired object that is not a pure function of `spec` never converges.** Including a timestamp, a random suffix or a value read from the live object makes `specEqual` false on every pass, so the controller issues an update on every reconcile forever.
- **A finalizer whose removal path can fail blocks deletion indefinitely.** If the external cleanup permanently errors, the object remains in terminating state and the namespace containing it cannot be deleted until the finalizer is removed by hand.
- **Relying on watch events alone hides the divergence the pattern exists to fix.** Without a periodic resync, a lost event leaves the cluster wrong until the next unrelated write to the object, and the bug appears intermittent.
- **Cluster-scoped RBAC granted for convenience widens the blast radius.** A controller with list and delete on all namespaces converts a scoping bug in one reconcile into deletion of objects it does not own.
