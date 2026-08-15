---
title: "Secure Boot v2 and Flash Encryption on the ESP32: a chain of trust you can't un-burn"
date: 2026-07-30
track: iot-embedded
summary: "Secure Boot v2 verifies your bootloader and app against an eFuse-burned public-key digest; flash encryption makes a dumped SPI chip useless. Both are one-way eFuse burns — here's how the chain of trust fits together, the RELEASE vs DEVELOPMENT split, and how to test without bricking a board."
reading_time: 6
tags: [esp32, esp-idf, secure-boot, flash-encryption, efuse, security]
sources:
  - title: "Secure Boot v2 — ESP-IDF Programming Guide (ESP32, stable)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/security/secure-boot-v2.html"
  - title: "Flash Encryption — ESP-IDF Programming Guide (ESP32, stable)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/security/flash-encryption.html"
  - title: "Flash Encryption — ESP-IDF Programming Guide (ESP32-S3)"
    url: "https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/security/flash-encryption.html"
  - title: "Secure Boot v2 — ESP-IDF Programming Guide (ESP32-C2, ECDSA)"
    url: "https://docs.espressif.com/projects/esp-idf/en/v5.0.3/esp32c2/security/secure-boot-v2.html"
  - title: "ESP32 Secure Boot and Flash Encryption — Zbotic"
    url: "https://zbotic.in/esp32-secure-boot-and-flash-encryption-protect-your-device/"
---

Two threats hit a field-deployed ESP32. Someone reflashes your bootloader with their own code, or someone desolders the flash chip and reads your firmware — API keys, business logic, the lot — straight off the SPI bus. Secure Boot v2 answers the first; flash encryption answers the second. They are separate features, they lean on the same eFuse hardware, and once you commit them in RELEASE mode there is no going back. This article is about wiring them up deliberately.

## The chain of trust

Secure Boot v2 builds a signature chain rooted in silicon. You generate an RSA-3072 keypair. The **private key stays on your build machine forever**; it never touches the device. At build time, ESP-IDF signs the second-stage bootloader and the app with it, appending an RSA-PSS signature block to each image. What goes into the chip is only the **SHA-256 digest of the public key**, burned into eFuse.

On boot, the immutable first-stage ROM bootloader verifies the second-stage bootloader's signature, which in turn verifies the app, before either runs. The public key travels *inside* the signed image — an attacker can read it, but they can't forge a signature without the private key, and they can't swap in their own key because the ROM checks the embedded key against the burned digest first. Break any link and the chip refuses to boot.

Chip families differ on the crypto and the eFuse layout:

| Chip | Signature scheme | Digest eFuse | Key slots |
|------|------------------|--------------|-----------|
| ESP32 (rev v3.0+) | RSA-3072 (RSA-PSS) | BLK2, `ABS_DONE_1` | 1 |
| ESP32-S3 / C3 / C6 | RSA-3072 (RSA-PSS) | `BLOCK_KEYx` + `SECURE_BOOT_DIGESTx`, `SECURE_BOOT_EN` | up to 3 |
| ESP32-C2 | ECDSA-256 (NIST P-256) | `BLOCK_KEY0` | 1 |

The three key slots on the newer RISC-V parts matter: you can burn multiple digests and revoke one with `KEY_REVOKEx` if a signing key leaks, without bricking the fleet. Classic ESP32 gives you exactly one shot.

## Flash encryption

Flash encryption is symmetric. A key is generated (by the device on first boot, or burned by you) into an eFuse key block that firmware cannot read back. The flash controller transparently decrypts on read and encrypts on write, so a dumped image is ciphertext.

- **Classic ESP32** uses AES-256 in a tweaked, block-offset-XORed mode over 32-byte blocks. The enable counter is `FLASH_CRYPT_CNT` (7 bits); an **odd** number of set bits means encryption is on.
- **ESP32-S3 / C3 / C6** use **XTS-AES** — 256-bit key (XTS-AES-128) in one `BLOCK_KEYx`, or 512-bit (XTS-AES-256) across two, tagged via `KEY_PURPOSE` as `XTS_AES_128_KEY`. The counter is `SPI_BOOT_CRYPT_CNT` (3 bits, odd = enabled).

Encryption covers the bootloader, partition table, and any partition marked `encrypted`, including your OTA app slots. The key never leaves the chip, so ciphertext from board A is meaningless on board B.

## DEVELOPMENT vs RELEASE — the distinction that matters

This is the single most important decision, and it is not reversible.

**DEVELOPMENT mode** keeps the door open. The serial (UART) bootloader can still re-encrypt and reflash plaintext images, so you can iterate over USB. `FLASH_CRYPT_CNT` / `SPI_BOOT_CRYPT_CNT` is left un-write-protected, giving a limited number of reflashes (three on classic ESP32). Perfect for a bench board; useless as production security, because that same serial path is what an attacker uses.

**RELEASE mode** slams it shut. UART-bootloader decryption is disabled, the crypt counter is write-protected, and the only remaining way to update firmware is OTA. Do this on a device you still need to debug and you have effectively locked yourself out.

## Doing it, step by step

Configure both in `idf.py menuconfig` under *Security features*:

```
CONFIG_SECURE_BOOT=y
CONFIG_SECURE_BOOT_V2_ENABLED=y
CONFIG_SECURE_BOOT_SIGNING_KEY="secure_boot_signing_key.pem"
CONFIG_SECURE_FLASH_ENC_ENABLED=y
CONFIG_SECURE_FLASH_ENCRYPTION_MODE_DEVELOPMENT=y   # or ..._RELEASE
```

Generate the signing key (keep it out of git, back it up offline):

```
espsecure.py generate_signing_key --version 2 secure_boot_signing_key.pem
```

Build the signed bootloader, then flash. ESP-IDF signs images and burns the eFuses on first boot:

```
idf.py bootloader
idf.py -p /dev/ttyUSB0 flash monitor
```

Before and after, dump the eFuse state so you know exactly what is committed:

```
espefuse.py -p /dev/ttyUSB0 summary
```

Look for `ABS_DONE_1` / `SECURE_BOOT_EN` and the crypt-count fields flipping to enabled. For a manual, auditable rollout you can also burn the key digest yourself with `espefuse.py burn_key`, but let the build system drive it the first few times.

> **Warning — eFuse burns are permanent.** eFuse bits go 0 → 1 only; there is no erase. Selecting RELEASE mode and flashing will irreversibly disable serial reflashing, and may disable JTAG and ROM download mode. A wrong menuconfig choice here does not throw an error — it produces a board you can only ever update over OTA, or a brick. Never let RELEASE mode near a board you can't afford to lose. Also: do **not** cut power during the first-boot encryption pass — an interrupted pass corrupts flash and forces a full reflash.

## How this meets OTA

Secure Boot and flash encryption change what an OTA update *is*. The [OTA updates article](/articles/iot-embedded/2026-07-26-esp32-ota-updates/) covers `esp_https_ota`, the A/B partition dance, and rollback. Layer security on top and three things follow:

1. **Every OTA image must be signed** with the same private key, or the new slot fails verification and the device stays on the old app. Your build pipeline, not just your bench, needs that key.
2. **OTA partitions are encrypted per-device.** The incoming image arrives as plaintext ciphertext-for-transit (protect it with HTTPS); the flash driver re-encrypts it with the device's own eFuse key as it writes. You ship one signed binary; each board stores its own ciphertext.
3. **In RELEASE mode, OTA is the only update path left.** That makes rollback and anti-rollback (`CONFIG_APP_ROLLBACK_ENABLE`, the `esp_ota_mark_app_valid_cancel_rollback` self-test) load-bearing — a bad push you can't fix over serial is a truck roll or a dead node.

Secure Boot and anti-rollback also compose: a signed-but-old image with a revoked security version is rejected, closing the downgrade-to-a-known-vuln attack.

## Getting the order right

Enable and validate Secure Boot v2 first, confirm signed images boot, then enable flash encryption — debugging an encrypted *and* signed image that won't boot is miserable when you can't tell which layer failed. Keep the two DEVELOPMENT-mode boards on your bench until the signed OTA path works end to end, because the moment you go RELEASE, that bench board becomes a field device you can only reach over the network.

**Try next:** Flash Secure Boot v2 + flash encryption in **DEVELOPMENT mode** on a sacrificial board, run `espefuse.py summary` before and after to watch the eFuses commit, push one signed OTA update to prove the pipeline, and only then consider RELEASE mode on hardware you're prepared to never touch over USB again.
