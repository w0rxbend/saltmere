---
title: "Shuffle sharding: fault isolation that plain sharding can't buy"
date: 2026-07-31
track: sys-patterns
summary: "Regular sharding contains a bad tenant to one shard — but everyone in that shard goes down with them. Shuffle sharding gives each tenant a random combination of nodes, so a single poison workload almost never fully overlaps anyone else. The magic is combinatorics."
reading_time: 5
tags: [sharding, fault-isolation, blast-radius, multi-tenancy, resilience]
sources:
  - title: "Workload isolation using shuffle-sharding — Amazon Builders' Library (Colm MacCárthaigh)"
    url: "https://aws.amazon.com/builders-library/workload-isolation-using-shuffle-sharding/"
  - title: "Shuffle Sharding: massive and magical fault isolation — AWS Architecture Blog"
    url: "https://aws.amazon.com/blogs/architecture/shuffle-sharding-massive-and-magical-fault-isolation"
  - title: "Shuffle sharding in Cortex/Mimir (worked example of the technique)"
    url: "https://cortexmetrics.io/docs/guides/shuffle-sharding/"
---

You run a fleet of 8 worker nodes behind a router, serving many tenants. One tenant sends a poison request — an expensive query, a retry storm, a bit of accidental DDoS — that pins whatever node handles it. What's your blast radius?

**No sharding:** the bad tenant can reach any node, so eventually *all 8* are degraded. Everyone is affected. **Plain sharding:** assign each tenant to one shard of, say, 2 nodes. Now the damage is contained to that shard — but every *other* tenant assigned to the same shard shares the tenant's fate. You've traded "everyone a little" for "a fixed group totally". For that group, it's an outage.

## The shuffle

Shuffle sharding, from the Amazon Builders' Library, changes how the shard is chosen. Instead of picking one of a few fixed shards, you give each tenant a **random combination** of nodes drawn from the whole fleet. With 8 nodes and a shard size of 2, there are C(8,2) = **28** possible pairs. Two tenants collide *completely* only if they were handed the exact same pair — and with a fault-tolerant client that retries the other node in its shard, a tenant is only fully knocked out when *every* node it holds is also held by the noisy tenant.

That "every node overlaps" event gets vanishingly rare as the numbers grow. Scale to 100 nodes with a shard size of 5: there are C(100,5) ≈ **75 million** combinations. Pick two tenants at random and the odds that all 5 of one's nodes fall inside the other's 5 are about 1 in 75 million. A single bad tenant might make one node hot for a handful of others — but almost nobody loses their *whole* shard, so almost everyone still has a healthy node to retry against.

A back-of-envelope way to see it: the chance a second tenant's shard is a subset of the first's is roughly C(k, k) / C(n, k) = 1 / C(n, k). Bigger fleet, or bigger shard, and the denominator explodes.

## Assigning shards deterministically

You don't store a table; you *derive* each tenant's shard from its ID so any router computes the same set. A simple virtual-node approach:

```python
import hashlib

def shard_for(tenant_id: str, nodes: list[str], shard_size: int) -> list[str]:
    chosen, pool = [], list(nodes)
    for i in range(shard_size):
        # seed the RNG deterministically from tenant + iteration
        h = hashlib.sha256(f"{tenant_id}:{i}".encode()).digest()
        idx = int.from_bytes(h[:8], "big") % len(pool)
        chosen.append(pool.pop(idx))     # sample WITHOUT replacement
    return chosen
```

Every router, given the same node list, produces the same shard for a tenant — no coordination — and the sampling-without-replacement guarantees `shard_size` *distinct* nodes. Route the tenant only to those nodes, and make the client retry within the shard on failure. That retry is not optional; it's the second half of the pattern. Overlap gives you partial redundancy, and the client is what cashes it in.

Two caveats. Shuffle sharding isolates *independent* failures — a poison tenant, a bad host. It does **not** save you from a bug that crashes every node the same way; that's a fleet-wide correlated failure, and no shard geometry helps. And shard size is a real dial: bigger shards mean more redundancy per tenant but more overlap between tenants, so you contain less. This is the same idea AWS Route 53 and Mimir/Cortex use in production, and it costs you nothing but a hash function.

**Try next:** take the function above, simulate 10,000 tenants over 100 nodes with shard size 5, mark one tenant's nodes as "down", and count how many other tenants lost *all* of their nodes versus lost *at least one*. The gap between those two numbers is exactly what shuffle sharding bought you.
