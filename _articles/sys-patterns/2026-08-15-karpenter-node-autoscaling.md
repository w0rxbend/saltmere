---
title: "Karpenter: Just-in-Time Nodes Instead of Node Groups"
date: 2026-08-15
track: sys-patterns
summary: "Cluster-autoscaler resizes pre-defined node groups; Karpenter throws the groups away and bin-packs pending pods straight onto freshly chosen instance types. Here's how the v1 NodePool and NodeClass CRDs work, what WhenEmptyOrUnderutilized consolidation actually does, how spot interruptions are handled natively, and how disruption budgets keep the optimizer from eating your capacity mid-day. Current as of the AWS provider v1.14 LTS, with Azure's NAP now GA."
reading_time: 5
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
  - title: "aws/karpenter-provider-aws — releases (v1.14 LTS)"
    url: "https://github.com/aws/karpenter-provider-aws/releases"
---

Cluster-autoscaler asks a constrained question: "which of my pre-defined node groups should grow by one?" Every group is a fixed instance type (or lookalike family), so you end up curating a zoo of ASGs and still waking up to pods stuck `Pending` because the one group they fit in hit quota. **Karpenter** inverts the model: it watches unschedulable pods, computes the cheapest set of instances that would fit them — choosing type, size, zone, and capacity type *per launch* from hundreds of options — and calls the cloud API directly. No node groups, no ASGs, nodes in roughly a minute.

Quick scope note: this is the node-level half of the story. The earlier KEDA article covered the pod-level half — KEDA/HPA decide *how many pods*, and Karpenter's job is making the nodes those pods need exist (and stop existing).

## Two CRDs: NodePool and NodeClass

The API graduated to **`karpenter.sh/v1`** with Karpenter 1.0 back in 2024 and has been stable since; the AWS provider is on the **v1.14 LTS** line as of mid-2026. Azure caught up: AKS ships Karpenter as **Node Auto-Provisioning (NAP)**, now generally available and the default posture in AKS Automatic, using the same `NodePool` CRD with an `AKSNodeClass`. (A community GCP provider exists but isn't at parity.)

The split is deliberate:

- **NodePool** (cloud-neutral): *constraints* — which architectures, capacity types, instance categories a node may be; plus limits, weights, taints, expiry, and disruption policy.
- **NodeClass** (cloud-specific: `EC2NodeClass` / `AKSNodeClass`): *how to build the machine* — AMI/image selection, subnets, security groups, block devices, user data.

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
      expireAfter: 720h           # recycle nodes monthly (patched AMIs)
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

The philosophy: constrain *loosely*. Every extra requirement shrinks the set of instance types Karpenter can price-shop across, which costs you both money and spot availability. Pods add their own scheduling constraints (nodeSelectors, topology spread, affinities) and Karpenter honors them at provisioning time — it simulates the scheduler before it buys anything.

## Consolidation: the part cluster-autoscaler never had

Provisioning is half the job; **consolidation** is the other half. With `consolidationPolicy: WhenEmptyOrUnderutilized`, Karpenter continuously looks for nodes it can delete (workloads fit elsewhere) or **replace with a cheaper node** (a half-empty `m6i.2xlarge` becomes an `m6i.large`). `WhenEmpty` is the conservative alternative: only reap nodes with no daemonset-exempt pods. `consolidateAfter` sets how long a node must be idle/underutilized before it's a candidate — raise it for bursty workloads that would otherwise thrash.

This is also where Karpenter fixes drift: change the AMI in your `EC2NodeClass` and nodes are progressively replaced (**Drifted**), no eksctl rolling update required.

| Disruption reason | Trigger |
|-------------------|---------|
| Empty / Underutilized | Consolidation math says delete or downsize |
| Drifted | Node no longer matches NodePool/NodeClass spec |
| Expired | `expireAfter` elapsed |

All *voluntary* disruption respects **budgets** (the `disruption.budgets` block above): rate limits by node count or percentage, optionally on a cron schedule, optionally scoped to specific reasons. Pod-level `PodDisruptionBudgets` and the `karpenter.sh/do-not-disrupt` annotation are honored too — budgets are how you let the optimizer run at 3 a.m. and sit still during peak.

## Spot without the sidecar

Karpenter handles **spot interruptions natively**: pointed at an SQS queue fed by EventBridge (spot interruption warnings, rebalance recommendations, scheduled maintenance), it reacts to the 2-minute notice by cordoning, draining, and pre-spinning a replacement — no `aws-node-termination-handler` DaemonSet needed. Interruption handling is *involuntary* disruption, so it deliberately ignores budgets: the node is dying either way. Combined with a wide instance-type allowlist, this makes "spot-first with on-demand fallback" a one-line policy (`capacity-type: ["spot", "on-demand"]`) instead of a fleet of weighted ASGs.

**Limits and weights** round out multi-pool setups: `limits` caps how much a pool may provision (a GPU pool that can't exceed 16 cards), and `weight` orders pools so workloads land on the reserved-instance pool before the spot pool, or on `general` before the tainted `system` pool.

Try next: create a second NodePool with `weight: 100`, spot-only, capped by `limits`, and watch `kubectl get nodeclaims -w` during a deploy — seeing Karpenter price-shop instance types in real time is the fastest way to build trust before you turn consolidation loose in prod.
