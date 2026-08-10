---
title: "Distributed Locking, Redlock, and the Fencing-Token Debate"
date: 2026-08-10
track: microservices
summary: "A single-node SET NX lock is easy to write and easy to get subtly wrong. The hard part isn't the happy path — it's what happens when a client pauses past the TTL. That question splits distributed locks into two camps: efficiency locks (Redlock is fine) and correctness locks (you need fencing tokens the resource actually checks)."
reading_time: 6
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

Mutual exclusion is a solved problem inside one process: a mutex, a `synchronized` block, a compare-and-swap. Stretch it across processes on different machines and every guarantee you leaned on quietly disappears. There is no shared memory, no scheduler that can suspend a thread that misbehaves, and — the part that trips almost everyone — no way to stop a process that already holds the lock from *thinking* it still holds it after it has actually lost it. Distributed locking is less about acquiring the lock and more about what happens in that gap.

## The naive Redis lock

The canonical single-node lock is one command. You set a key only if it is absent, stamp it with a value nobody else can guess, and give it a time-to-live so a crashed holder can't wedge the lock forever:

```
SET resource_name my_random_value NX PX 30000
```

`NX` makes the write conditional on the key not existing — that's the mutual exclusion. `PX 30000` is a 30-second auto-expiry — that's the deadlock avoidance. `my_random_value` is a per-acquisition nonce (a UUID, or 20 bytes from `/dev/urandom`), and it matters enormously at release time.

The tempting release is `DEL resource_name`. It's wrong. Consider: your lock's TTL expires while you're mid-work, Redis frees it, another client acquires it, and *then* your slow `DEL` fires — deleting a lock you no longer own. So release has to be a compare-and-delete: check the value is still yours, and only then delete. That read-then-delete must be atomic, which on Redis means a Lua script (the server runs it without interleaving other commands). This is the exact script from the Redis docs:

```lua
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
```

Called with `KEYS[1] = resource_name` and `ARGV[1] = my_random_value`. Now you only ever delete your own lock. (Redis 8.4+ folds this into a single `DELEX key IFEQ my_random_value` command, but the Lua form is what you'll be asked to write in an interview.)

This lock is correct against crashes and correct against slow releases. What it is *not* correct against is a paused holder — and that failure mode is the whole story.

## The failure mode that TTLs can't fix

Client 1 acquires the lock with a 30-second TTL and starts working. Then it stalls: a stop-the-world GC pause, a hypervisor descheduling the VM, a blocked syscall, a laptop lid closing. The pause runs past 30 seconds. Redis expires the key. Client 2 acquires the very same lock and proceeds. Client 1 wakes up — with no idea any time has passed — and finishes its operation, still believing it holds the lock. **Two clients now act on the resource at once.**

No TTL setting fixes this, because the pause is unbounded and the client can't know it happened. A shorter TTL makes it *more* likely; a longer TTL makes a genuine crash wedge the lock for longer. The lock service did nothing wrong. The problem is that the lock is advisory: it tells the client "you hold it," and a paused client hears a stale answer.

## Redlock

Redlock is antirez's algorithm for locking without a single point of failure, across N independent Redis masters (typically 5) that don't replicate to each other. To acquire, a client:

1. Records the current time.
2. Tries to `SET ... NX PX` the same key + nonce on all N nodes sequentially, each with a *tiny* per-node timeout (5–50 ms) so a dead node can't stall the whole attempt.
3. Considers the lock held only if it got a **majority (N/2 + 1)** *and* the total elapsed time is less than the TTL.
4. Sets the lock's effective validity to `TTL − elapsed − clock_drift`.
5. On failure, unlocks every node — including ones it thinks it didn't get.

The majority quorum means the lock survives a minority of nodes crashing, and no single Redis being restarted (and losing the key) breaks safety. It's a genuinely clever design. But it addresses *node* failure, not *client* pauses — step 4's validity accounting protects the acquire phase, not the arbitrarily long gap between "I hold it" and "I write."

## Kleppmann's critique: fencing tokens

Martin Kleppmann's 2016 post "How to do distributed locking" makes the argument that reframed the whole topic. He splits locks into two purposes:

- **Efficiency** — the lock just avoids doing the same work twice (a duplicate email, a redundant expensive computation). A rare double-execution costs you a little money or an apology. Here, he says, "it is unnecessary to incur the cost and complexity of Redlock" — a single-node lock is fine.
- **Correctness** — a double-execution corrupts data: "corrupted file, data loss, permanent inconsistency, the wrong dose of a drug administered to a patient." Here you need the lock to *never* let two holders write.

His key result: **no lock service alone can give you correctness**, because of the pause scenario above. Redlock, he notes, "does not have any facility for generating fencing tokens." The fix has to move to the resource. A **fencing token** is a monotonically increasing number the lock hands out on each successful acquisition. Every write to the protected resource carries its token, and the resource **rejects any token lower than the highest it has already seen**:

```python
# Storage server enforces the fence; the lock only mints the token.
def write(payload, token):
    if token <= storage.max_seen_token:   # a stale, paused holder
        raise StaleLockError(f"token {token} <= {storage.max_seen_token}")
    storage.max_seen_token = token        # persisted atomically with the write
    storage.apply(payload)
```

Now replay the pause. Client 1 acquires with token 33, pauses, and loses the lock. Client 2 acquires with token 34 and writes. Client 1 wakes and writes with token 33 — and the storage server rejects it, because it has already accepted 34. The lock became advisory again, but it no longer matters: the resource is the one enforcing exclusion, and it does so with a rule that's immune to timing. This is the same "make the resource the arbiter" instinct behind [idempotency keys](/articles/microservices/2026-07-30-idempotency-keys-safe-retries) — instead of trusting the caller's claim, the server checks a monotonic guard it controls.

## antirez's rebuttal

antirez replied in "Is Redlock safe?" He doesn't dispute that fencing works; he questions the premises. If your resource can check a monotonic token, he argues, it can just as well check a **large random token** as a compare-and-set — you don't strictly need monotonicity, and either way the resource does the real enforcement. He also defends Redlock's timing model: after acquiring the majority the algorithm *re-checks* it hasn't run out of validity. The honest reading: for **efficiency** locks the two barely disagree, and for **correctness** locks both end up saying the resource must guard its own writes. The dispute is about how safe an unfenced lock is in practice, not whether fencing is the mechanism.

## The correctness-oriented alternative: lease-based locks

If you need correctness, reach for a coordination service built on consensus. **ZooKeeper**'s lock recipe has each client create a *sequential ephemeral* znode under a lock node; the client holding the lowest sequence number owns the lock, and everyone else watches the next-lower node rather than stampeding. Two properties fall out for free: the znode is *ephemeral*, so a client's session dying (even a paused client whose session times out) releases the lock automatically, and the ever-increasing sequence number (or the znode's `zxid`) *is* a natural fencing token. **etcd** does the equivalent with a lease-backed lock; each key carries a `revision`, and that monotonic revision serves as the fence a resource can check. **Chubby**, Google's lock service, pioneered this lease-plus-sequencer shape. These systems are slower than a Redis `SET` and require a quorum to be up — that's the price of a lock whose guarantees survive the pause.

## Choosing

Ask one question: *what breaks if two clients hold the lock at once?* If the answer is "we waste some work," a single-node `SET NX PX` with the Lua release is the right amount of engineering, and Redlock buys availability if that node's failure worries you. If the answer is "we corrupt data or double-charge a customer," no lock is enough on its own — you need fencing tokens the resource enforces, and a lease-based lock (ZooKeeper, etcd, Chubby) is a cleaner place to get monotonic tokens than bolting them onto Redis. The lock schedules; the resource decides.

**Try next:** take the fencing example above and add token persistence, then simulate a paused holder — sleep past the TTL between acquire and write — and confirm the storage server rejects the stale token while the newer holder's write succeeds.
