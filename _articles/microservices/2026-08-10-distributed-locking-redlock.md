---
title: "Distributed Locking, Redlock, and the Fencing-Token Debate"
date: 2026-08-10
track: microservices
summary: "A single-node SET NX lock is short to write and subtly easy to get wrong. The hard case is not the happy path but a client that pauses past the time-to-live. That case splits distributed locks into two categories: efficiency locks, for which Redlock suffices, and correctness locks, which require fencing tokens the protected resource itself checks."
reading_time: 7
tags: [distributed-locks, redis, redlock, zookeeper, etcd, consistency]
sources:
  - title: "Distributed Locks with Redis (Redlock) — Redis docs"
    url: "https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/"
  - title: "How to do distributed locking — Martin Kleppmann"
    url: "https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html"
  - title: "Is Redlock safe? — antirez (Salvatore Sanfilippo)"
    url: "http://antirez.com/news/101"
  - title: "ZooKeeper Recipes and Solutions — Locks"
    url: "https://zookeeper.apache.org/doc/current/recipes.html"
  - title: "Why etcd — etcd docs (distributed locking, revisions)"
    url: "https://etcd.io/docs/v3.5/learning/why/"
---

**Gist.** Mutual exclusion across machines has no shared memory and no scheduler that can suspend a misbehaving holder, so a lock service can only tell a client that it *held* the lock at some past instant. A time-to-live (TTL) bounds the damage from a crashed holder, and a quorum algorithm such as Redlock removes the single point of failure, but neither prevents a paused holder from writing after its lease expired. The only construction that closes that gap moves enforcement to the protected resource — a monotonically increasing **fencing token** checked on every write — at the cost of modifying the resource itself.

## The single-node Redis lock

The canonical single-node lock is one command: set a key only if it is absent, stamp it with a value no other client can guess, and attach an expiry so a crashed holder cannot wedge the lock permanently.

```
SET resource_name my_random_value NX PX 30000
```

`NX` makes the write conditional on the key not existing — that is the mutual exclusion. `PX 30000` sets a 30-second auto-expiry — that is the deadlock avoidance. `my_random_value` is a per-acquisition nonce (a UUID, or 20 bytes read from `/dev/urandom`), and it is load-bearing at release time.

Releasing with `DEL resource_name` is incorrect. The interleaving that breaks it: the TTL expires while the holder is still working, Redis frees the key, a second client acquires it, and only then does the first client's slow `DEL` arrive, **deleting a lock held by someone else**. Release must therefore be a compare-and-delete: verify the stored value is still the caller's nonce, then delete. The read and the delete must be atomic, which on Redis means a Lua script, since the server runs a script without interleaving other commands. The Redis documentation gives exactly this script:

```lua
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
```

It is invoked with `KEYS[1] = resource_name` and `ARGV[1] = my_random_value`, so a client can only ever delete its own acquisition. Recent Redis versions also expose a conditional delete that folds the comparison and the deletion into one command, removing the need for the script.

This lock is correct against holder crashes and against slow releases. It is not correct against a **paused** holder.

## The failure mode a TTL cannot fix

Client 1 acquires the lock with a 30-second TTL and begins work. It then stalls: a stop-the-world garbage-collection pause, a hypervisor descheduling the virtual machine, a blocked system call, a suspended laptop. The stall exceeds 30 seconds. Redis expires the key. Client 2 acquires the same lock and proceeds. Client 1 resumes with no indication that time has passed, completes its operation, and still believes it holds the lock. **Two clients act on the resource concurrently.**

No TTL value eliminates this, because the pause is unbounded and invisible to the paused process. A shorter TTL makes the overlap more likely; a longer TTL leaves a genuinely crashed holder's lock wedged for longer. The lock service behaved correctly throughout. The defect is that the lock is **advisory**: it reports a fact about the past, and a resumed client reads that report as a fact about the present.

## Redlock

Redlock is antirez's algorithm for locking without a single point of failure, running across N independent Redis masters — typically 5 — that do not replicate to one another. Acquisition proceeds as follows.

1. Record the current time.
2. Attempt `SET ... NX PX` with the same key and nonce on all N nodes sequentially, each attempt bounded by a small per-node timeout (5–50 ms) so an unreachable node cannot stall the whole acquisition.
3. Treat the lock as held only if a **majority (N/2 + 1)** of nodes accepted it *and* total elapsed time is below the TTL.
4. Set the lock's effective validity to `TTL − elapsed − clock_drift`.
5. On failure, release on every node, including those believed not to have granted it.

The majority quorum means the lock tolerates a minority of node failures. Restarts are a subtler matter: an instance that restarts without a durable copy of the key forgets the acquisition it granted, and enough such restarts let a second client assemble its own majority for the same key. The Redis documentation addresses this by requiring that a crashed instance stay unavailable for at least the TTL before rejoining — a *delayed restart* — so that every lock it had granted has expired everywhere by the time it accepts new acquisitions. The algorithm addresses **node** failure. It does not address **client** pauses: the validity accounting in step 4 constrains the acquire phase, not the interval between the moment the client concludes it holds the lock and the moment its write reaches the resource.

## Kleppmann's critique: fencing tokens

Martin Kleppmann's 2016 post "How to do distributed locking" separates locks by purpose.

- **Efficiency.** The lock exists to avoid repeating work — a duplicate email, a redundant expensive computation. A rare double execution costs money or an apology. For this case Kleppmann writes that "it is unnecessary to incur the cost and complexity of Redlock"; a single-node lock suffices.
- **Correctness.** A double execution damages data — Kleppmann's examples are a corrupted file, lost data, permanent inconsistency, and the wrong dose of a drug administered to a patient. Here two holders must never both write.

His central claim is that **no lock service alone provides correctness**, precisely because of the pause scenario above; of Redlock specifically he notes that it "does not have any facility for generating fencing tokens." The enforcement must move to the resource. A **fencing token** is a monotonically increasing number issued on each successful acquisition. Every write to the protected resource carries its token, and the resource **rejects any token lower than the highest it has already accepted**, persisting that high-water mark atomically with the write.

Replaying the pause with fencing: client 1 acquires token 33, pauses, and loses the lock. Client 2 acquires token 34 and writes, advancing the high-water mark to 34. Client 1 resumes and writes with token 33, and the resource rejects it. The lock remains advisory, but that no longer matters, because exclusion is enforced by a comparison that does not depend on timing. This is the same structure as [idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries): rather than trusting the caller's claim, the server checks a guard it owns.

### Implementation sketch (Scala)

The load-bearing parts are the quorum count with validity accounting on the client side, and the monotonic guard on the resource side.

```scala
final case class Lease(nonce: String, token: Long, validityMs: Long)

def acquire(nodes: List[RedisNode], key: String, ttlMs: Long): Option[Lease] =
  val nonce = java.util.UUID.randomUUID().toString
  val start = System.nanoTime()
  // Per-node timeout keeps one unreachable node from consuming the whole TTL.
  val granted = nodes.count(_.setNxPx(key, nonce, ttlMs, timeoutMs = 50))
  val elapsedMs = (System.nanoTime() - start) / 1000000
  val validity = ttlMs - elapsedMs - clockDriftMs
  if granted >= nodes.size / 2 + 1 && validity > 0 then
    Some(Lease(nonce, mintToken(), validity))
  else
    nodes.foreach(_.releaseIfValueEquals(key, nonce)) // includes nodes believed to have refused
    None

// Enforcement lives with the resource, not the lock service.
final class FencedStore(persist: (Array[Byte], Long) => Unit):
  private var highWaterMark: Long = 0L
  def write(payload: Array[Byte], token: Long): Either[String, Unit] = synchronized {
    if token <= highWaterMark then Left(s"stale token $token <= $highWaterMark")
    else
      // The mark and the payload must reach durable storage in one transaction.
      persist(payload, token)
      highWaterMark = token
      Right(())
  }
```

`mintToken` must return values that increase across acquisitions; the following section describes services that supply such values directly.

## antirez's rebuttal

antirez responded in "Is Redlock safe?". He does not dispute that fencing works; he disputes the premises. If a resource is capable of checking a monotonic token, he argues, it is equally capable of checking a **large random token** by compare-and-set, so strict monotonicity is not required, and in either construction the resource performs the actual enforcement. He also defends Redlock's timing model, noting that after obtaining the majority the algorithm re-checks that validity has not been exhausted. For **efficiency** locks the two positions differ little, and for **correctness** locks both conclude that the resource must guard its own writes. The disagreement concerns how safe an unfenced lock is in practice, not whether fencing is the mechanism.

## Lease-based locks on consensus systems

Where correctness is required, a coordination service built on consensus supplies both the lease and the token. **ZooKeeper**'s lock recipe has each client create a *sequential ephemeral* znode beneath a lock node; the client owning the lowest sequence number holds the lock, and each other client watches the next-lower znode rather than all contending on one notification. Two properties follow: the znode is *ephemeral*, so the expiry of a client's session — including a paused client's session — releases the lock, and the increasing sequence number (or the znode's `zxid`) serves as a fencing token. **etcd** provides a lease-backed lock in which each key carries a `revision`, and that monotonic revision is the value a resource can fence on. **Chubby**, Google's lock service, established this lease-plus-sequencer shape. These systems require a quorum to be available and are slower than a single Redis `SET`; that is the cost of a lock whose token survives the pause.

## Choosing

The deciding question is what breaks when two clients hold the lock simultaneously. If the answer is duplicated work, a single-node `SET NX PX` with the Lua compare-and-delete release is proportionate, and Redlock adds availability when the failure of that one node is the concern. If the answer is corrupted data or a double charge, no lock is sufficient on its own: the resource must check fencing tokens, and a lease-based lock (ZooKeeper, etcd, Chubby) issues monotonic tokens directly rather than requiring them to be constructed on top of Redis. The lock schedules; the resource decides.

## Pitfalls

- **Releasing with `DEL` instead of a compare-and-delete.** Symptom: a client's lock disappears while it is still working. Cause: a previous holder's delayed release deleted a key that had already expired and been re-acquired.
- **Performing the compare and the delete as two client round trips.** Symptom: the same cross-holder deletion as above, now rarer and harder to reproduce. Cause: the key can expire and be re-acquired between the `GET` and the `DEL`; only a server-side script or a single conditional-delete command makes the pair atomic.
- **Reusing a fixed lock value across acquisitions.** Symptom: every client passes the ownership check. Cause: the compare-and-delete distinguishes owners solely by the nonce, so a constant value makes all holders indistinguishable.
- **Extending the TTL to "avoid" the pause problem.** Symptom: after a real crash the resource is unavailable for the full extended TTL. Cause: the TTL trades crash-recovery latency against overlap probability; it cannot bound an unbounded pause.
- **Treating Redlock's quorum as protection against paused clients.** Symptom: two holders write despite a five-node deployment. Cause: the quorum and validity accounting cover node failure and acquisition time, not the interval between acquisition and the write.
- **Minting fencing tokens on the client.** Symptom: an older holder's token exceeds a newer holder's and the resource accepts the stale write. Cause: monotonicity must come from a single ordering authority — a ZooKeeper sequence number, an etcd revision — not from independent clients.
- **Persisting the token high-water mark separately from the payload.** Symptom: after a crash the resource accepts a token it had already fenced out. Cause: if the mark and the write are not committed atomically, recovery can restore one without the other.
