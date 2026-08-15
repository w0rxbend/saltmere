---
title: "systemd-creds: Encrypting Secrets to the TPM Instead of Baking Them Into a Unit"
date: 2026-08-15
track: linux-tools
summary: "A database password in Environment= leaks into every `systemctl show`, every core dump, and /proc/PID/environ. systemd-creds encrypts a secret to the host TPM2, drops the ciphertext next to the unit, and hands the plaintext to exactly one service as a read-only file under $CREDENTIALS_DIRECTORY. Here's the full flow on systemd 258."
reading_time: 6
tags: [systemd, credentials, tpm2, secrets, security]
sources:
  - title: "systemd-creds(1) — systemd manual"
    url: "https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html"
  - title: "systemd.exec(5) — Credentials section"
    url: "https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#Credentials"
  - title: "System and Service Credentials — systemd.io"
    url: "https://systemd.io/CREDENTIALS/"
  - title: "systemd Encrypted Service Credentials — systemshardening.com"
    url: "https://www.systemshardening.com/articles/linux/systemd-credentials-hardening/"
---

There is a bad habit baked into a lot of unit files: `Environment=DB_PASSWORD=hunter2`, or an `EnvironmentFile=` pointing at a `0644` file in `/etc`. Both leak. Environment variables show up in `systemctl show`, in `/proc/PID/environ` (readable by anything running as that user), and in core dumps. A world-readable secrets file is exactly what its permissions say it is. **systemd credentials** are the built-in answer: a secret is encrypted at rest — optionally bound to the machine's **TPM2** so the ciphertext is useless on any other host — and delivered to one service as a read-only file the rest of the system can't see. As of **systemd 258** (released September 2025) the whole workflow is a couple of commands.

## Encrypting a secret

`systemd-creds encrypt` reads plaintext and writes an authenticated ciphertext blob. The `--name=` matters: the name is embedded in the blob and checked at decrypt time, so a credential encrypted as `db-password` can't be silently swapped in where a unit expects `api-token`.

```bash
# has-tpm2 tells you what backing key is available
systemd-creds has-tpm2        # -> "yes" if the firmware TPM2 is usable

echo -n 'hunter2' | systemd-creds encrypt --name=db-password - db-password.cred
shred -u -           # the plaintext never needs to touch disk
```

The `--with-key=` option chooses what the encryption key is derived from:

| `--with-key=` | Bound to | Blob is portable? |
|---------------|----------|-------------------|
| `host` (`-H`) | secret in `/var/lib/systemd/credential.secret` | copies with `/var` |
| `tpm2` (`-T`) | the machine's TPM2 chip | no — dies with the TPM |
| `host+tpm2` | both, required together | no |
| `auto` (default) | TPM2 if present + host key if `/var` is persistent | no, in practice |

The default `auto` gives you `host+tpm2` on a normal bare-metal box with a TPM: the ciphertext can only be decrypted **on this machine, by this OS install**. Copy `db-password.cred` to another host and `systemd-creds decrypt` fails — the TPM there derives a different key. That is the property `Environment=` can never give you.

## Referencing it from a unit

Point the unit at the ciphertext. `LoadCredentialEncrypted=` loads the blob, decrypts it at service start, and exposes the plaintext:

```ini
[Service]
ExecStart=/usr/bin/myapp
LoadCredentialEncrypted=db-password:/etc/creds/db-password.cred
```

The three directives worth knowing, in increasing convenience:

- **`LoadCredential=name:path`** — plaintext file, no decryption. Fine for a non-secret config value; still delivered privately (see below), just not encrypted at rest.
- **`LoadCredentialEncrypted=name:path`** — the same, but the file is a `systemd-creds` blob and gets decrypted on load. This is the workhorse.
- **`SetCredentialEncrypted=name:blob`** — the ciphertext lives *inline in the unit file*, no separate file to manage. Generate it with `-p`:

```bash
echo -n 'hunter2' | systemd-creds encrypt -p --name=db-password - -
```

That prints a ready-to-paste `SetCredentialEncrypted=db-password: \` stanza with the base64 blob, which you drop straight into `[Service]`. The unit is now self-contained and safe to commit — the secret is ciphertext bound to your TPM.

## Reading it from the service

At runtime systemd decrypts the credential into a **tmpfs** under `/run/credentials/<unit>` and sets `$CREDENTIALS_DIRECTORY` to point there. Your service just opens a file:

```bash
#!/usr/bin/env bash
# inside ExecStart
password="$(cat "$CREDENTIALS_DIRECTORY/db-password")"
```

In the unit you can hand the path to a program that wants `--password-file` using the `%d` specifier, which expands to `$CREDENTIALS_DIRECTORY`:

```ini
ExecStart=/usr/bin/myapp --password-file=%d/db-password
```

What you get is deliberately locked down. The credential files are **read-only, mode `0400`, owned by the service's user**; the directory is `0700`. It lives on unswappable tmpfs, so the plaintext never hits disk or swap. It is **not** inherited by child processes' environments, and it's scoped to this one unit — another service, even as the same user, has its own `$CREDENTIALS_DIRECTORY` and cannot see this one. When the service stops, the tmpfs is torn down.

You can prove the round-trip without writing a unit at all:

```bash
systemd-run -P --wait \
  -p LoadCredentialEncrypted=db-password:"$PWD/db-password.cred" \
  systemd-creds cat db-password
```

That spins up a transient service, decrypts the blob, and `systemd-creds cat` prints the credential from inside `$CREDENTIALS_DIRECTORY` — the same code path a real service uses.

## The honest caveats

TPM binding protects the secret **at rest**, not once it's decrypted: any code running as the service user can read `$CREDENTIALS_DIRECTORY` while the service is up, exactly like `fscrypt` protects when the key is present but not who touches the plaintext after. If an attacker already has code execution as your service, credentials don't save you — they shrink the blast radius of a *stolen unit file or backup*, not of a live compromise. Pure `tpm2` binding is also brittle: a firmware update or clearing the TPM changes the key and your secret is unrecoverable, so keep an escrow copy of the plaintext somewhere safe or use `host+tpm2` and back up `/var/lib/systemd/credential.secret`. And a credential without `--name=` set can be replayed under a different name — always name them. Within those limits, it's strictly better than the environment variable it replaces: encrypted at rest, machine-bound, private to one service, and gone from memory the moment the service exits.

**Try next:** run `systemd-creds has-tpm2`; if it says `yes`, encrypt a throwaway secret with `--with-key=tpm2`, copy the `.cred` file to another machine, and watch `systemd-creds decrypt` fail there — that failure *is* the TPM binding, demonstrated.
