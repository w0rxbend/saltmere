---
title: "jj: Jujutsu, the Git-Compatible VCS That Rethinks the Working Copy"
date: 2026-08-14
track: linux-tools
summary: "Jujutsu keeps Git's storage but removes the index, the detached-HEAD state and the category of uncommitted work. The model — working copy as a commit, first-class conflicts, change IDs, and an undoable operation log — with a walkthrough that runs against an existing Git repository."
reading_time: 6
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

**Gist.** Git's user-facing model splits work across three states — working tree, index (staging area) and commit history — and several common operations (rebase, conflict resolution, history rewriting) are only partially recoverable once they go wrong. Jujutsu (the `jj` command, a version-control system started by Martin von Zweigbergk at Google) keeps Git's on-disk object storage but replaces that model with three mechanisms: **the working copy is itself a commit**, **conflicts are stored inside commits rather than halting an operation**, and **every repository mutation is appended to an operation log that can be reversed**. The cost is that all of this is additional state layered over the Git repository: a `.jj` directory that must stay consistent with `.git`, a second identifier namespace (change IDs alongside commit IDs), and a pre-1.0 tool whose command-line interface carries no stability guarantee between releases.

The release referenced throughout is **v0.43.0**. The project is self-hosting — jj is developed in jj.

## The working copy is a commit

Git distinguishes three locations for in-progress work: the file tree on disk, the index, and the commit graph. jj collapses these. The working copy corresponds to a commit, referred to by the symbol `@`, and **every `jj` command begins by snapshotting the on-disk file tree into that commit**. There is no `git add` step and no staged/unstaged distinction, because there is no intermediate location for a change to occupy.

The consequence is uniformity rather than convenience. Since the edits on disk are already part of a commit, **the category "uncommitted work" does not exist**, and operations defined over commits — rebase, squash, split — apply to the working-copy commit exactly as they apply to any ancestor. Git requires the working tree to be clean, or stashed, before many of the same operations.

The snapshot step is also the main invariant to keep in mind: state on disk is captured at command invocation, not continuously. A file written while no `jj` command runs is recorded by the next command that runs.

## Change IDs and commit IDs

Each change carries two identifiers with different stability properties.

- The **commit ID** is an ordinary Git object hash. It is a function of the commit's content and parents, so any rewrite — amending a description, rebasing onto a new base — produces a different commit ID.
- The **change ID** is jj-specific and **stable across rewrites**. It identifies "the same logical change" as its content and position move.

This is what makes history editing tractable to describe: a rebase of a stack rewrites every commit ID in it, while the change IDs used to refer to those commits in subsequent commands remain valid. Under Git the equivalent references become stale the moment the rewrite completes.

## Conflicts as commit content

In Git a conflicting merge or rebase stops mid-operation: the repository enters a state (`REBASE_HEAD` and friends) in which most other commands are refused until the conflict is resolved or the operation aborted.

jj records a conflict as data in the resulting commit. **A rebase always runs to completion**; where a change does not apply cleanly, the conflicted state is stored in the commit it produced. Resolution becomes an ordinary edit to that commit, which may be deferred, and further changes may be rebased on top of a conflicted commit in the meantime. The failure mode this trades into is a quieter one: a conflict that halts nothing can be carried forward and pushed unnoticed, so the presence of conflicted commits must be checked for rather than discovered by being blocked.

## Anonymous branches

jj does not require a name for every line of development. A stack of changes may exist with no branch name attached — **anonymous branches** — with jj tracking the heads. Named bookmarks exist and are what gets exchanged with a Git remote, so a name is needed at push time rather than at creation time.

## The operation log

Every command that modifies the repository appends an entry to the **operation log**, viewable with `jj op log`. This is distinct from Git's reflog, which records the movement of individual references: an operation-log entry covers **the whole repository state, including the working copy**, and is recorded atomically per command.

Two commands consume it. `jj undo` reverses the most recent operation. `jj op restore <id>` returns the entire repository to the state recorded at an earlier operation. A rebase that produced the wrong result is therefore undone as one step rather than reconstructed from reflog entries per reference.

## Colocated repositories

No conversion or re-import is required to evaluate jj on an existing project. `jj git init --colocate` creates a `.jj` directory alongside the existing `.git`, and both tools operate on the same commits.

```bash
# Install (Rust toolchain) — or use a distro/Homebrew package
cargo install --locked --bin jj jj-cli
jj --version                       # -> jj 0.43.0

# Attach jj to an existing clone, leaving .git operational
cd my-project
jj git init --colocate

# Start a new change on top of main and describe it
jj new main -m "wip: parse config"
$EDITOR src/config.rs              # edits are snapshotted into @ by the next command

# Refine the description, then inspect the graph
jj describe -m "feat: parse TOML config"
jj log                             # change IDs, commit IDs, @ marker

# Fold the whole of @ into its parent commit (add -i to select hunks)
jj squash

# Reverse the whole preceding operation
jj undo
jj op log                          # audit every repository-state change
```

`jj git push` sends bookmarks to the remote and `jj git fetch` retrieves from it, so collaborators using Git see an ordinary Git repository.

A useful evaluation is to colocate jj into a repository whose history is already familiar and restrict usage to `jj new`, `jj describe`, `jj squash` and `jj undo` for a single feature branch. That subset exercises the working-copy-as-commit model and the operation log without depending on the parts of the interface that are still changing between releases.

## Pitfalls

- **Files written between commands are invisible until a command runs.** Snapshotting happens at `jj` invocation, so a script that inspects jj state and then writes files leaves those writes unrecorded until the next `jj` command.
- **A conflicted commit does not stop the workflow.** Because rebases complete regardless, conflict markers can be carried into descendant commits and onto a bookmark that is then pushed; nothing refuses the operation on the way.
- **Change IDs are not portable to Git tooling.** Continuous integration, review systems and `git` itself address commits by hash only, so a change ID cannot be used to identify a commit outside jj.
- **A colocated repository has two tools mutating one object store.** Running Git commands that rewrite history in a colocated repository leaves jj to reconcile the result at its next snapshot, rather than the two views updating together.
- **Pre-1.0 status applies to the interface, not only the internals.** Command behaviour observed on v0.43.0 is not guaranteed by any stability policy across later releases, and release notes routinely record breaking command-line changes.
- **A bookmark is still required to publish.** Anonymous branches remove naming from local work but not from the Git remote, so an unnamed stack has nothing for `jj git push` to send until a bookmark is created.
