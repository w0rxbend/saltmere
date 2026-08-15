---
title: "systemd-creds: Encrypting Secrets to the TPM Instead of Baking Them Into a Unit"
date: 2026-08-15
track: linux-tools
summary: "A database password in Environment= is visible in systemctl show, in core dumps, and in /proc/PID/environ. systemd-creds encrypts the secret to the host Trusted Platform Module (TPM2), stores the ciphertext beside the unit, and delivers the plaintext to exactly one service as a read-only file under $CREDENTIALS_DIRECTORY. The full flow, from encryption to delivery."
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

**Gist.** Secrets passed to a service through `Environment=` or a world-readable `EnvironmentFile=` are exposed to `systemctl show`, to `/proc/PID/environ`, and to core dumps, because the environment block is process metadata rather than protected storage. systemd credentials replace that channel: the secret is stored as an authenticated ciphertext, optionally bound to the host's Trusted Platform Module (TPM2) so the blob decrypts on no other machine, and the plaintext is materialised at service start as a mode-`0400` file in a private in-memory file system. The cost is a key that lives outside the backup: a TPM2-bound blob becomes unrecoverable if the TPM state changes, so the scheme trades recoverability for machine binding.

## The exposure being closed

The environment block of a process is readable through `/proc/PID/environ` by anything running as that user, is reported verbatim by `systemctl show`, and is captured in core dumps. A secret placed there therefore has an audience considerably larger than the one service that needs it. A plaintext `EnvironmentFile=` with permissive modes has whatever audience its permission bits describe. Neither channel restricts the secret to a single unit, and neither protects it at rest.

## Encrypting a secret

`systemd-creds encrypt` reads plaintext and writes an authenticated ciphertext blob. **The `--name=` value is embedded in the blob and verified at decrypt time**, so a credential encrypted as `db-password` is rejected where a unit loads it under a different name.

```bash
# has-tpm2 reports whether TPM2 support is available; "yes", "partial" or "no"
systemd-creds has-tpm2

# reading plaintext from stdin keeps it off disk
echo -n 'hunter2' | systemd-creds encrypt --name=db-password - db-password.cred
```

`--with-key=` selects what the encryption key is derived from:

| `--with-key=` | Bound to | Blob is portable? |
|---------------|----------|-------------------|
| `host` (`-H`) | secret in `/var/lib/systemd/credential.secret` | copies with `/var` |
| `tpm2` (`-T`) | the machine's TPM2 chip | no — dies with the TPM |
| `host+tpm2` | both, required together | no |
| `auto` (default) | `host+tpm2` where a TPM2 is available and `/var/lib/systemd` is on persistent media, otherwise `host` | depends on which it resolved to |

On bare metal with a TPM and a persistent `/var`, the default `auto` resolves to `host+tpm2`, and **decryption then requires both the on-disk host secret and the same TPM**. Copying `db-password.cred` to another host and running `systemd-creds decrypt` fails there: the other machine's TPM cannot recover the key the blob was sealed against. That non-portability is the property the environment block cannot provide.

## Referencing the blob from a unit

`LoadCredentialEncrypted=` names the credential, points at the ciphertext, and decrypts it at service start:

```ini
[Service]
ExecStart=/usr/bin/myapp
LoadCredentialEncrypted=db-password:/etc/creds/db-password.cred
```

Three directives cover the useful cases:

- **`LoadCredential=name:path`** — a plaintext file, no decryption. Suitable for a non-secret configuration value; the delivery is still private to the unit, merely not encrypted at rest.
- **`LoadCredentialEncrypted=name:path`** — the same, where the file is a `systemd-creds` blob decrypted on load.
- **`SetCredentialEncrypted=name:blob`** — the ciphertext is stored inline in the unit file, with no separate file to manage. The `-p` flag emits the stanza:

```bash
echo -n 'hunter2' | systemd-creds encrypt -p --name=db-password - -
```

This prints a `SetCredentialEncrypted=db-password: \` stanza carrying the base64 blob, for pasting into `[Service]`. The unit is then self-contained, and the embedded value is ciphertext bound by whichever key `--with-key=` selected on the encrypting host.

## Delivery to the service

At runtime systemd decrypts the credential into a **`ramfs` instance mounted under `/run/credentials/<unit>`** and sets `$CREDENTIALS_DIRECTORY` to that path. The service reads a file:

```bash
#!/usr/bin/env bash
# inside ExecStart
password="$(cat "$CREDENTIALS_DIRECTORY/db-password")"
```

For a program that accepts a path rather than a value, the `%d` specifier expands to `$CREDENTIALS_DIRECTORY`:

```ini
ExecStart=/usr/bin/myapp --password-file=%d/db-password
```

The resulting invariants are narrow and worth stating exactly. **Credential files are read-only, mode `0400`, owned by the service's user; the containing directory is `0700`.** `ramfs` pages, unlike `tmpfs` pages, are never swapped, so the plaintext reaches neither disk nor swap. The value is **not placed in the environment**, and so is not inherited by child processes through the environment block. The directory is scoped to the unit: **a different service running as the same user receives its own `$CREDENTIALS_DIRECTORY` and cannot read this one**. When the service stops, the mount is torn down.

The round trip can be verified without writing a unit:

```bash
systemd-run -P --wait \
  -p LoadCredentialEncrypted=db-password:"$PWD/db-password.cred" \
  systemd-creds cat db-password
```

This starts a transient service, decrypts the blob, and prints the credential from inside `$CREDENTIALS_DIRECTORY` over the same code path a permanent unit uses.

## What the binding does not cover

TPM binding protects the secret **at rest, not after decryption**. While the service runs, any code executing as the service user can read `$CREDENTIALS_DIRECTORY`. The limit is the same one `fscrypt` has: both protect data while the key is absent, and neither constrains who touches the plaintext once the key is present. Credentials therefore reduce the value of a stolen unit file or a stolen backup; they do not contain an attacker who already has code execution as the service.

The encrypt, reference and read steps are the three commands shown above; the directives and specifiers used here are those documented in the current `systemd-creds(1)` and `systemd.exec(5)` pages.

## Pitfalls

- **A blob encrypted with pure `tpm2` binding is unrecoverable after the TPM changes.** A firmware update or a TPM clear alters the derived key, and no copy of the plaintext exists elsewhere; retain an escrow copy, or use `host+tpm2` and back up `/var/lib/systemd/credential.secret`.
- **A `host`-bound blob travels with `/var`.** Because the key material is the file `/var/lib/systemd/credential.secret`, anyone who obtains a backup of `/var` together with the ciphertext can decrypt it.
- **A credential encrypted with `--name=""` can be loaded under any name.** The name check that rejects a substituted blob only exists when a name was embedded at encryption time, and an explicitly empty name embeds none; omitting the option instead takes the name from the output file's name, so writing the blob to stdout with `-` leaves nothing to derive it from.
- **Falling back to `Environment=` for the same value defeats the whole arrangement.** The secret is then in `/proc/PID/environ` and in `systemctl show` regardless of the encrypted copy on disk.
- **Reading the credential into an exported shell variable re-creates the exposure.** The file itself is private to the unit, but an exported value is inherited by every child process and appears in that process's environment block.
