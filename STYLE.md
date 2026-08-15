# House style

Every article follows this. It is the contract shared by the publishing job and
by any editing pass, so that 459 articles read as one voice rather than 459.

## Register

Write as a working researcher writes for peers: precise, economical, impersonal.

- **Third person.** No "you", "your", "we", "let's". Not "you'll want an index
  here" but "the query requires a covering index".
- **No filler.** Delete "just", "simply", "actually", "really", "basically",
  "obviously", "of course", "it turns out", "the good news is". If a claim
  needs "obviously", it needs a proof instead.
- **No hype.** No "blazing fast", "game-changer", "magic". State the measured
  effect: "reduces p99 from 240 ms to 31 ms".
- **Define terms on first use,** including expansions of every acronym:
  "write-ahead log (WAL)", "highest random weight (HRW) hashing".
- **Quantify.** Prefer a bound, a complexity class, or a measurement to an
  adjective. "O(log n) seeks" beats "fast lookups".
- **Attribute.** Name the paper, RFC, JEP or KIP where a result comes from, and
  put it in `sources` as well.
- **Hedge honestly.** Where evidence is thin, say so plainly: "no published
  benchmark separates these under contention".

Clarity outranks formality. Short declarative sentences in the active voice are
academic; convoluted passive constructions are not. Never sacrifice a concrete
example to sound serious.

## Evidence discipline

This is the rule that matters most, and the one an editing pass is most likely
to break. Depth must come from explaining mechanisms that are already
established, never from manufacturing new facts.

- **Do not invent precision.** A specific number, bound, version, chapter
  reference, percentage or date is only admissible if it is already in the
  article or verifiable in a cited source. Vague-but-true beats specific-but-
  invented every time.
- **Do not invent rationale.** Documentation records what a system does; it
  rarely records why its designers chose it. Write "Klipper delimits
  expressions with single braces" — not a reconstructed motive for the choice.
- **Prefer the weaker true statement.** If "none of the 2^16 − 2 interleavings
  is a valid page" is only true when every sector differs, write the bound
  instead. If a vendor documents "keys are removed after 24 hours", do not
  promote it to "retained for at least 24 hours" — that inverts the guarantee.
- **Describe the real mechanism.** "The second inserter blocks on the first
  transaction's speculative-insertion lock" is a claim about observable
  behaviour; "the engine takes an index-level lock on the key's slot" is a
  guess dressed as detail.
- **Never add a source you have not read.** Correcting a stale source *title*
  is welcome; inventing a citation to support a new claim is not.

When an edit cannot be supported, cut the claim. A shorter accurate article is
worth more than a longer confident one.

## Structure

    ---
    front matter (unchanged: title, date, track, summary, reading_time, tags, sources)
    ---

    **Gist.** Two or three sentences: the problem, the mechanism that solves it,
    and the cost that mechanism imposes. A reader who stops here should still be
    able to state the trade-off correctly.

    ## Substantive sections

    Depth first. Derive the bound, walk the state machine, name the failure
    mode. Highlight the load-bearing detail in **bold** so it survives skimming.

    ### Implementation sketch (Scala)     ← where the topic admits code

    ## Pitfalls

    A short list of the specific traps, each one sentence of symptom plus one of
    cause.

## Code

Code exists to make a mechanism legible, not to be copied into production.

- **Gists, not solutions.** Twenty to forty lines showing the load-bearing idea.
  Omit imports, configuration, error plumbing and logging unless they *are* the
  point. An ellipsis comment (`// ... unchanged`) is preferable to twenty lines
  of scaffolding.
- **Scala 3** syntax for new examples, standard library only unless the article
  is about a specific library. Prefer immutable structures and explicit types on
  public signatures.
- **Runnable in principle.** No pseudocode dressed as Scala; no invented APIs.
- **Comment the non-obvious line only.** A comment restating the code is noise.
- Keep an existing example in another language when the article is *about* that
  ecosystem (a Klipper macro, a Prometheus rule, an ESP-IDF C snippet). Scala is
  added where it illuminates an algorithm or a distributed-systems mechanism —
  not bolted onto an article about 3D-printer firmware.

## Pitfalls sections

State the trap, not the advice. "Deleting a key while iterating invalidates the
iterator" is useful; "be careful with iterators" is not.
