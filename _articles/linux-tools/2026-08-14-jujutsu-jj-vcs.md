---
title: "jj: Jujutsu, the Git-Compatible VCS That Rethinks the Working Copy"
date: 2026-08-14
track: linux-tools
summary: "Jujutsu keeps Git's storage but throws out the index, the detached-HEAD dance and the fear of losing work. Here's the model — working copy as a commit, first-class conflicts, change IDs, and an undoable operation log — plus a walkthrough you can run on a real Git repo today."
reading_time: 5
tags: [jujutsu, jj, git, version-control, developer-tools, linux-tools]
sources:
  - title: "jj-vcs/jj — Jujutsu, a Git-compatible VCS (GitHub)"
    url: "https://github.com/jj-vcs/jj"
  - title: "Jujutsu documentation — tutorial and reference"
    url: "https://jj-vcs.github.io/jj/latest/"
  - title: "Steve Klabnik's Jujutsu Tutorial"
    url: "https://steveklabnik.github.io/jujutsu-tutorial/"
  - title: "Release v0.43.0 · jj-vcs/jj"
    url: "https://github.com/jj-vcs/jj/releases/tag/v0.43.0"
---

Git won the version-control war a decade ago, so the interesting question now is not "what replaces Git?" but "what can we build *on top of* Git's storage that's nicer to drive?" **Jujutsu** — the `jj` command, started by Martin von Zweigbergk at Google — is the most convincing answer so far. It speaks Git's on-disk format fluently, pushes and pulls to GitHub like any other client, and then hands you a working model that quietly deletes several of Git's most error-prone concepts.

As of mid-August 2026 the current release is **v0.43.0** (2 July 2026). It's pre-1.0 and still labels itself experimental, but it is self-hosting — jj is developed in jj — and increasingly used on real teams.

## The working copy *is* a commit

Git's mental model has three places for your work: the working tree, the index (staging area), and commits. jj collapses this. **Your working copy is itself a commit**, and every time you run a `jj` command it automatically snapshots whatever is on disk into that commit. There is no `git add`, no `git stash`, no "unstaged vs staged" split. The file you just edited is already in a commit — the one jj calls `@`, the working-copy commit.

This sounds like a small ergonomic tweak but it changes everything downstream. Because your edits are *always* committed, there is no such thing as losing uncommitted work, and every operation (rebase, squash, split) works on commits uniformly, including the one you're sitting in.

## Change IDs vs commit IDs

Each jj change has two identifiers. The **commit ID** is a normal Git SHA — it changes whenever the commit's content changes (a rebase, an amend). The **change ID** is a stable, jj-only identity that survives all that rewriting. So you can keep referring to "that change" while its underlying SHA mutates underneath you. This is what makes jj's fearless history editing safe to reason about.

## First-class conflicts and anonymous branches

In Git a conflict is a crisis: the operation halts and you must resolve before doing anything else. In jj a conflict is just *data stored in a commit*. A rebase always completes; if a change doesn't apply cleanly, the conflict is recorded in the resulting commit and you resolve it whenever you like — or rebase further work on top and resolve at the end. Nothing blocks.

jj also drops the requirement to name every branch. You can create a stack of changes with no branch name at all — **anonymous branches** — and jj tracks the heads for you. Named bookmarks exist for when you push to a Git remote, but day-to-day you rarely create one.

## The operation log and `jj undo`

Every command that changes the repo is recorded in the **operation log** (`jj op log`). This is not the reflog — it's a complete, atomic history of *the whole repository state*, including the working copy. Made a mess with a bad rebase? `jj undo` reverses the last operation; `jj op restore <id>` jumps the entire repo back to any earlier point. This is the feature people miss most when they go back to Git.

## Colocated Git repos: try it on a real project

You don't convert anything. `--colocate` creates a `.jj` directory *next to* the existing `.git`, so Git and jj see the same commits and you can run either tool.

```bash
# Install (Rust toolchain) — or use a distro/Homebrew package
cargo install --locked --bin jj jj-cli
jj --version                       # -> jj 0.43.0

# Point jj at an existing clone, keeping .git working
cd my-project
jj git init --colocate

# Start a new change on top of main; describe it
jj new main -m "wip: parse config"
$EDITOR src/config.rs              # edits are auto-snapshotted into @

# Refine the description, then see the graph
jj describe -m "feat: parse TOML config"
jj log                             # change IDs, commit IDs, @ marker

# Fold @ down into its parent (like an interactive squash)
jj squash

# Made a mistake? Reverse the whole last operation.
jj undo
jj op log                          # audit every repo-state change
```

`jj git push` sends your bookmarks to the remote; `jj git fetch` pulls. Your teammates on plain Git never know.

**Try next:** colocate jj into a repo you already know well, then use only `jj new`, `jj describe`, `jj squash` and `jj undo` for one feature branch — the moment `jj undo` cleanly rewinds a rebase you botched is the moment the model clicks.
