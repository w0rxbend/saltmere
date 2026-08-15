---
title: "The ambassador pattern: a smart connection for a simple client"
date: 2026-07-24
track: sys-patterns
summary: "An ambassador is an out-of-process proxy that owns the details of reaching a remote service — sharding, retries, transport security, request routing — so the application opens one plain connection to localhost and remains oblivious."
reading_time: 6
tags: [ambassador, envoy, sharding, proxy, patterns, burns]
sources:
  - title: "Brendan Burns, Designing Distributed Systems (2nd ed.) — Ambassadors"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/ch04.html"
  - title: "Envoy: Life of a Request"
    url: "https://www.envoyproxy.io/docs/envoy/latest/intro/life_of_a_request"
  - title: "Kubernetes: Sidecar Containers"
    url: "https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/"
---

**Gist.** An application that must reach a partitioned, authenticated, or partially canaried backend accumulates connection logic — shard arithmetic, retry policy, mutual TLS (mTLS), traffic splitting — that has nothing to do with its domain. The **ambassador** pattern moves that logic into a separate process colocated with the application, which then opens a single unencrypted connection to `localhost` and treats the backend as one flat endpoint. The cost is a second process in the deployment unit, one additional network hop per request, and a proxy configuration that becomes load-bearing: a mistake in it is a production outage with no trace in the application's source tree.

## Position relative to the sidecar

A sidecar container augments the application container in place, without changing it. The ambassador applies that arrangement to outbound traffic: a process in the same pod, sharing the pod's network namespace, that the application reaches over the loopback interface. Because the two containers share a namespace, `localhost:6379` in the application resolves to a listener in the ambassador — **no service discovery, no DNS resolution, and no network authentication are involved on that hop**. Kubernetes sidecar containers are documented as containers running alongside the main container in the same pod; the ambassador is that mechanism applied to egress.

The invariant the pattern rests on is a clean one: **the application's view of the backend is a single address with no topology**. Everything topological — how many shards exist, which endpoints are healthy, which fraction of traffic goes to a canary revision, what certificate is presented upstream — lives on the far side of that loopback socket. Rebuilding the application image is not required to change any of it.

## A sharded cache the application cannot see

Consider a Redis deployment split into shards and a service that must route each key to the shard that owns it. Placing the shard arithmetic in the application couples the release cycle of every language runtime in the fleet to the cache topology. An ambassador removes that coupling:

```yaml
apiVersion: v1
kind: Pod
metadata: { name: leaderboard }
spec:
  containers:
    - name: app                      # connects to redis at localhost:6379
      image: leaderboard:2.1
    - name: redis-ambassador
      image: envoyproxy/envoy:v1.34-latest
      args: ["-c", "/etc/envoy/envoy.yaml"]
      ports: [{ containerPort: 6379 }]
      volumeMounts:
        - { name: cfg, mountPath: /etc/envoy, readOnly: true }
  volumes:
    - { name: cfg, configMap: { name: redis-ambassador-cfg } }
```

Envoy provides a native Redis proxy network filter that hashes each key onto an upstream shard. The application issues `redis.get("score:42")` against `localhost:6379`; the ambassador computes the placement:

```yaml
# envoy.yaml (excerpt)
filters:
  - name: envoy.filters.network.redis_proxy
    typed_config:
      "@type": type.googleapis.com/envoy.extensions.filters.network.redis_proxy.v3.RedisProxy
      stat_prefix: redis
      prefix_routes: { catch_all_route: { cluster: redis_shards } }
# redis_shards cluster uses MAGLEV/ring-hash lb_policy across the shard endpoints
```

Changing the shard count, substituting the hash policy, or adding transport security to the backend connection alters only the ConfigMap. The application image is unchanged.

The load balancing policies named above are the substance of the mechanism. **Ring-hash and Maglev are consistent-hashing policies: on a membership change the only keys that change owner are those reassigned to or from the endpoint that arrived or left.** A naive modulo mapping over *n* endpoints, by contrast, remaps most of the key space when *n* changes — on a shrink from *n* to *n*−1 buckets only about one key in *n* keeps its owner — which for a cache means near-total miss traffic against the backing store at the moment of a resize. Consistent hashing bounds the disturbance to the keys of the endpoint that left or arrived.

## What the additional hop buys

The ambassador is the egress half of what a **service mesh** performs: one proxy terminates inbound mTLS and also load-balances, retries, and circuit-breaks outbound calls. Two further consequences follow from the invariant rather than from any specific proxy.

First, substitutability of the upstream. Pointing the ambassador at a mock backend in a development environment and at the real cluster in production is a configuration difference, not a code path guarded by an environment variable inside the application.

Second, uniformity across a polyglot fleet. A shard-aware client otherwise has to be implemented, tested, and kept current in every language the fleet uses. In the ambassador arrangement **that logic exists once, in one process, and every runtime consumes it through an ordinary socket**.

The counterweight is that the pattern is unjustified for a single flat backend reached over a plain connection: the hop adds latency and the configuration adds a failure surface without displacing any application logic. The pattern earns its cost when the *connection* carries real logic that would otherwise leak into the application.

### Implementation sketch (Scala)

The routing decision the ambassador owns, reduced to its load-bearing part: a hash ring with virtual nodes, and a lookup that finds the first ring position at or after the key's hash.

```scala
import scala.collection.immutable.TreeMap

final class HashRing private (ring: TreeMap[Int, String]):
  /** First endpoint clockwise from the key's position; wraps at the ring's end. */
  def endpointFor(key: String): Option[String] =
    if ring.isEmpty then None
    else ring.rangeFrom(HashRing.hash(key)).headOption.orElse(ring.headOption).map(_._2)

  def withEndpoint(endpoint: String): HashRing =
    HashRing(ring ++ HashRing.positions(endpoint).map(_ -> endpoint))

  def withoutEndpoint(endpoint: String): HashRing =
    HashRing(ring -- HashRing.positions(endpoint))

object HashRing:
  private val VirtualNodes = 160   // more replicas per endpoint, flatter key distribution

  private def hash(s: String): Int = scala.util.hashing.MurmurHash3.stringHash(s)

  private def positions(endpoint: String): Seq[Int] =
    (0 until VirtualNodes).map(i => hash(s"$endpoint#$i"))

  def empty: HashRing = HashRing(TreeMap.empty)
```

Removing an endpoint deletes only its own positions, so **only keys whose successor position belonged to that endpoint change owner**; every other key resolves to the same place as before. That property, not the hashing itself, is why the resize of a cache behind an ambassador does not evict the whole key space.

## Pitfalls

- **The proxy configuration is untested code.** A shard list or route prefix that is wrong sends live traffic to the wrong upstream, and no application test suite exercises it because the file lives in a ConfigMap.
- **Startup ordering.** If the application container begins issuing requests before the ambassador's listener is bound, connections to `localhost` are refused rather than queued; the symptom is a burst of connection errors confined to the first seconds after a pod starts.
- **The loopback hop is unauthenticated.** Any other process able to reach the pod's network namespace reaches the ambassador's listener with the ambassador's upstream credentials, because the application-side connection carries none of its own.
- **Modulo placement disguised as consistent hashing.** Selecting a load balancing policy that maps keys by endpoint index rather than by ring position causes a near-total remap on any membership change; the symptom is a cache hit rate collapsing at the instant a shard is added or a single endpoint fails health checks.
- **Silent shard failure looks like a cold cache.** When one upstream endpoint is ejected, its share of keys is redistributed to neighbours on the ring, so the application observes misses and elevated backing-store load rather than errors.
- **Per-request cost applies to every request.** The extra hop is paid on the hot path, so a workload dominated by very small, very frequent operations pays proportionally more of its latency budget to the ambassador than a workload of larger, rarer requests.
