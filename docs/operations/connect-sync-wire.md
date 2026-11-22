# Connect Sync wire format (ADR-054 Phase 1)

Frozen by `src/luma-connect-sync/tests/test_vectors.py`. Strings below are
UTF-8; `\0` is a zero byte; `‖` is concatenation; b64 is base64url without
padding.

| Value | Construction |
| --- | --- |
| Device keys | Ed25519 (sign), X25519 (box), raw 32-byte public keys |
| Key proof (Hub-checked) | Ed25519 over `luma-sync-device-v1\0<account>\0<device>\0<b64 box_pub>` |
| Bundle | canonical JSON (sorted keys, no spaces) `{v:1, account, epoch, ask, names, recovery_pub, members:{device: b64 sign_pub}, prev}` |
| `prev` | HMAC-SHA256(HKDF(previous ask, `luma-sync-epoch-chain-v1`), bundle body without `prev`); empty at epoch 1 |
| Envelope | HPKE base, X25519 / HKDF-SHA256 / ChaCha20-Poly1305, info `luma-sync-envelope-v1\0<account>\0<target>\0<epoch>` (target a device id or `recovery`) |
| Envelope signature | Ed25519 over `luma-sync-envelope-sig-v1\0<account>\0<target>\0<epoch>\0` ‖ SHA-256(ciphertext) |
| Member cert | HMAC(HKDF(ask, `luma-sync-member-v1`), account \0 device \0 sign_pub \0 box_pub \0 epoch) |
| Zone id | first 16 bytes, hex, of HMAC(HKDF(names, `luma-sync-zone-id-v1`), account \0 label) |
| Record name | hex HMAC(HKDF(names, `luma-sync-record-name-v1`), zone \0 name) |
| Blob id | hex HMAC(HKDF(names, `luma-sync-blob-id-v1\0<zone>`), plaintext chunk) |
| Zone key wrap | XChaCha20-Poly1305 under ask, AD `luma-sync-zone-key-v1\0<account>\0<zone>\0<generation>\0<epoch>`; nonce ‖ ciphertext |
| Record | zlib(canonical `{n, v}`) sealed with the zone key of its generation, AD `luma-sync-record-v1\0<account>\0<zone>\0<record name>\0<generation>\0<version>` |
| Blob (one ciphertext, any source) | generation (4 bytes BE) ‖ nonce (24) ‖ XChaCha20-Poly1305(kind (0 raw, 1 zlib) ‖ length (4 BE) ‖ payload ‖ zero padding), AD `luma-sync-blob-v1\0<account>\0<zone>\0<blob id>\0<generation>`; total a multiple of 65536 |
| Chunk boundaries | gear rolling hash, table entry i = first 8 bytes BE of HMAC(HKDF(names, `luma-sync-chunk-v1\0<zone>`), `luma-sync-gear-v2` ‖ i); 64 KiB minimum, 4 MiB maximum, cut where the low 20 bits are 0 |
| Join commitment | SHA-256(`luma-sync-join-commit-v1\0` ‖ device nonce) |
| Approvers' half | HMAC(HKDF(ask, `luma-sync-join-v1`), account \0 join id \0 device \0 commitment) |
| Join code | first 8 bytes BE of SHA-256(`luma-sync-join-code-v1` \0 account \0 device \0 sign_pub \0 box_pub \0 device nonce \0 approvers' half) mod 10^6, 6 digits |
| Recovery key | 24 BIP-39 English words = 256 bits ‖ 8-bit SHA-256 checksum |
| Recovery X25519 key | HKDF(Argon2id(entropy, salt = SHA-256(`luma-recovery-v1\0<account>`)[:16], t=3, m=64 MiB, p=4, 32 bytes), `luma-sync-recovery-x25519-v1`) |

Reserved for bundle v2: `transport_pub` (the iroh transport key, certified by
the identity key).
