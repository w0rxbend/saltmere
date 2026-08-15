---
title: "Karpenter: Just-in-Time Nodes Instead of Node Groups"
date: 2026-08-15
track: sys-patterns
summary: "Cluster-autoscaler resizes pre-defined node groups; Karpenter discards the groups and bin-packs pending pods onto freshly chosen instance types. This article covers the v1 NodePool and NodeClass custom resources, what WhenEmptyOrUnderutilized consolidation does, how spot interruptions are handled natively, and how disruption budgets bound the optimizer's effect on capacity, using the AWS provider and Azure's Node Auto-Provisioning as the two reference implementations."
reading_time: 6
tags: [karpenter, kubernetes, autoscaling, spot, eks, aks]
sources:
  - title: "Karpenter — NodePools (concepts)"
    url: "https://karpenter.sh/docs/concepts/nodepools/"
  - title: "Karpenter — Disruption: consolidation, budgets, interruption handling"
    url: "https://karpenter.sh/docs/concepts/disruption/"
  - title: "Amazon EKS Best Practices — Karpenter"
    url: "https://docs.aws.amazon.com/eks/latest/best-practices/karpenter.html"
  - title: "Azure — Node Auto-Provisioning (NAP) in AKS"
    url: "https://learn.microsoft.com/en-us/azure/aks/node-auto-provisioning"
  - title: "aws/karpenter-provider-aws — releases"
    url: "https://github.com/aws/karpenter-provider-aws/releases"
---

**Gist.** The Kubernetes cluster-autoscaler can only answer a constrained question — which of a set of pre-defined node groups should grow by one — so each workload shape requires its own curated group, and a pod remains `Pending` when the single group it fits is at quota. Karpenter replaces the groups with a solver: it observes unschedulable pods, simulates the scheduler to compute a set of instances that would accommodate them, selects instance type, size, zone and capacity type per launch, and calls the cloud provider application programming interface (API) directly. The cost is that the cluster's node inventory becomes a continuously re-optimised, mutable population rather than a set of stable groups, so node churn must be bounded explicitly by disruption budgets, pod disruption budgets and annotations.

This article covers the node-level half of autoscaling. The pod-level half — Kubernetes Event-Driven Autoscaling (KEDA) and the Horizontal Pod Autoscaler (HPA) deciding pod counts — is covered separately; Karpenter's responsibility is making the nodes those pods require exist, and cease to exist.

## Two custom resources: NodePool and NodeClass

The API graduated to **`karpenter.sh/v1`** with Karpenter 1.0 in 2024, and the AWS provider has released on the `v1.x` line since. Azure Kubernetes Service (AKS) ships Karpenter as **Node Auto-Provisioning (NAP)**, the node-provisioning mode used by AKS Automatic, with the same cloud-neutral `NodePool` custom resource paired with an `AKSNodeClass`. A community Google Cloud Platform provider also exists.

The two resources separate two different kinds of statement:

- **NodePool** (cloud-neutral) declares *constraints*: which architectures, capacity types and instance categories a node may have, plus limits, weights, taints, expiry and disruption policy.
- **NodeClass** (cloud-specific: `EC2NodeClass` or `AKSNodeClass`) declares *how the machine is built*: machine image selection, subnets, security groups, block devices, user data.

```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: general
spec:
  weight: 10                      # preferred over lower-weight pools
  limits:
    cpu: "500"                    # hard cap: pool stops provisioning here
    memory: 1000Gi
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 2m
    budgets:
      - nodes: "20%"              # normally: disrupt at most 20% at once
      - nodes: "0"                # business hours: no voluntary disruption
        schedule: "0 9 * * mon-fri"
        duration: 9h
  template:
    spec:
      expireAfter: 720h           # recycle nodes monthly (patched images)
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      requirements:
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot", "on-demand"]   # spot-first, on-demand fallback
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64", "arm64"]
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["c", "m", "r"]
        - key: karpenter.k8s.aws/instance-generation
          operator: Gt
          values: ["4"]
```

The requirements list is a filter, and it is the load-bearing knob: **every additional requirement narrows the set of instance types the solver may price-shop across**, reducing both the achievable price and the pool of spot capacity from which a launch can be satisfied. Constraining loosely at the NodePool level therefore has a direct effect on availability. Pod-level constraints — node selectors, topology spread constraints, affinities — are added by the workloads themselves, and **Karpenter simulates scheduling against those constraints before it launches anything**, so a node is not created unless the pending pods would in fact bind to it.

## Consolidation

Provisioning is one half of the controller; **consolidation** is the other. Under `consolidationPolicy: WhenEmptyOrUnderutilized`, Karpenter continuously searches for two kinds of move: deleting a node whose workloads fit on remaining nodes, and **replacing a node with a cheaper one** — a half-empty `m6i.2xlarge` becoming an `m6i.large`. The conservative alternative, `WhenEmpty`, only reclaims nodes that hold no pods other than daemonset pods.

`consolidateAfter` sets how long a node must remain empty or underutilised before it becomes a candidate. It is the damping term: **with a short value and a bursty workload, the controller removes capacity that the next burst immediately re-provisions**, so the setting trades cost efficiency against churn.

The same disruption machinery reconciles configuration changes. Changing the machine image reference in the `EC2NodeClass` marks existing nodes **Drifted**, and they are progressively replaced without an external rolling-update procedure.

| Disruption reason | Trigger |
|-------------------|---------|
| Empty / Underutilized | Consolidation determines the node can be deleted or downsized |
| Drifted | Node no longer matches the NodePool or NodeClass spec |
| Expired | `expireAfter` has elapsed |

Consolidation and drift are **voluntary** disruption, rate-limited by the `disruption.budgets` block: a bound expressed as a node count or a percentage, optionally attached to a cron schedule with a duration, optionally scoped to specific reasons (`Empty`, `Underutilized`, `Drifted`). Expiration is not one of those reasons — in `v1` an expired node begins draining rather than waiting for a replacement to be launched first. A budget of `nodes: "0"` over a scheduled window suspends voluntary disruption entirely for that window. Pod-level `PodDisruptionBudget` objects and the `karpenter.sh/do-not-disrupt` annotation are also honoured, so three independent mechanisms can each block a candidate node.

## Spot interruption handling

Karpenter handles **spot interruptions natively**. Pointed at an Amazon Simple Queue Service (SQS) queue fed by EventBridge — spot interruption warnings, rebalance recommendations, scheduled maintenance events — it reacts to the two-minute interruption notice by cordoning and draining the node and provisioning a replacement, without a separate `aws-node-termination-handler` DaemonSet.

The invariant worth stating explicitly: **interruption handling is involuntary disruption, and therefore ignores disruption budgets**. The node is being reclaimed by the provider regardless of what the budget says; honouring the budget would only delay the drain, not save the node. A budget of zero during business hours consequently stops consolidation and drift replacement, but does not stop spot reclamation.

Combined with a wide instance-type allowlist, this reduces a spot-first, on-demand-fallback posture to a single requirement (`capacity-type: ["spot", "on-demand"]`) rather than a set of weighted autoscaling groups. The width of the allowlist matters here for the same reason as above: a narrow list concentrates the cluster in fewer spot pools.

**Limits and weights** govern multi-pool clusters. `limits` caps the total resources a pool may provision — a graphics processing unit (GPU) pool bounded to a fixed number of cards — and provisioning from that pool stops at the cap. `weight` orders pools during scheduling simulation, so workloads land on a reserved-instance pool before a spot pool, or on a general pool before a tainted system pool.

## Pitfalls

- **Over-constrained requirements produce `Pending` pods that look like a Karpenter failure.** Narrowing instance category, generation, architecture and zone simultaneously can leave a set of instance types that is empty or has no spot capacity; the solver has nothing to launch.
- **A short `consolidateAfter` on a bursty workload causes node thrash.** Nodes are removed during the trough and re-provisioned during the next peak, paying launch latency and image-pull cost repeatedly.
- **A zero-node disruption budget during business hours does not stop spot reclamation.** Budgets bound voluntary disruption only; involuntary interruption proceeds, so workloads must still tolerate node loss during the protected window.
- **`karpenter.sh/do-not-disrupt` on a long-lived pod blocks voluntary disruption of the whole node.** Consolidation and drift replacement skip that node, so an annotated pod can retain an outdated machine image until something else removes the node.
- **Changing the NodeClass machine image marks every node in scope as Drifted at once.** The replacement rate is governed solely by the disruption budget; without one, the cluster replaces nodes as fast as pod disruption budgets permit.
- **A pool `limits` cap is a provisioning stop, not a scheduling error.** Once the cap is reached the pool silently stops creating nodes and pods stay `Pending`, which presents identically to a capacity shortage.
