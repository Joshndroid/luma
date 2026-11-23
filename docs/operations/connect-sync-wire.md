# Connect Sync wire format 2 (ADR-054 Phase 1)

Frozen by `src/luma-connect-sync/tests/test_vectors.py`. Strings are UTF-8;
`\0` is a zero byte; `‖` is concatenation; b64 is base64url without padding.
`lp(a, b, …)` is length-prefixed concatenation: each field as a 4-byte
big-endian length then its bytes (integers as decimal text).
`mac(k, …)` is HMAC-SHA256(k, lp(…)). Canonical JSON is sorted keys, no
spaces. Format 1 (Phase 1 before the security review) is not readable by
format 2 and was never used for real data.

| Value | Construction |
| --- | --- |
| Device keys | Ed25519 (sign), X25519 (box), raw 32-byte public keys |
| Key proof (Hub-checked) | Ed25519 over `luma-sync-device-v1\0<account>\0<device>\0<b64 box_pub>` |
| Recovery key | 24 BIP-39 English words = 256 bits ‖ 8-bit SHA-256 checksum |
| Recovery root | Argon2id(entropy, salt = SHA-256(`luma-recovery-v1\0<account>`)[:16], t=3, m=64 MiB, p=4, 32 bytes) |
| Recovery keys | X25519 = HKDF(root, `luma-sync-recovery-x25519-v1`); Ed25519 auth seed = HKDF(root, `luma-sync-recovery-auth-v1`); the auth public key is registered on the Hub |
| Bundle | canonical JSON `{v:2, account, epoch, ask, names, recovery_pub, recovery_auth_pub, members:{device:{sign, box}}, removed:[…], holds_recovery:[…], signer, prev, sig}` |
| `prev` | HMAC-SHA256(HKDF(previous ask, `luma-sync-epoch-chain-v2`), canonical bundle body without `prev`, `sig`); empty at epoch 1 |
| `sig` | the signer's Ed25519 over `luma-sync-bundle-v2\0` ‖ canonical(body ‖ prev); signer is a member of the previous epoch and not removed; empty at epoch 1 |
| Accepting epoch N+1 | chain and signature as above; `removed` ⊇ previous `removed`; every member was a member of epoch N (bundle or signed statement) with the same keys; nothing in the device's own removed list signs or is a member |
| Envelope | HPKE base, X25519 / HKDF-SHA256 / ChaCha20-Poly1305, info lp(`luma-sync-envelope-v2`, account, target, epoch); target a device id or `recovery` |
| Envelope signature (Hub-checked) | Ed25519 over `luma-sync-envelope-sig-v1\0<account>\0<target>\0<epoch>\0` ‖ SHA-256(ciphertext), by the wrapping device |
| Membership statement (Hub-checked) | canonical `{v:1, kind:"add", account, epoch, device, sign_pub, box_pub, via, signer, envelope_sha256, cert_sha256}`, signed Ed25519 over `luma-sync-membership-v1\0` ‖ canonical; `via` `approval` (signer = approving member) or `recovery` (signer `recovery`, signed by the recovery auth key) |
| Members of epoch N | the bundle's `members` plus every statement of epoch N whose signer is a member (or the recovery key), minus removed devices |
| Request signature (Hub-checked) | every Sync request but GET and `PUT devices/self` carries `X-Luma-Sync-Time` (unix seconds, ±300 s) and `X-Luma-Sync-Signature` = b64 Ed25519 by the device key over `luma-sync-request-v1\0<device>\0<METHOD>\0<path after /api/sync/v1/, with query>\0<time>\0` ‖ SHA-256(body); the bearer token alone changes nothing |
| Delete-account proof (Hub-checked) | recovery auth key Ed25519 over `luma-sync-delete-account-v1\0<account>\0<unix seconds>`, within 10 minutes |
| Member cert | mac(HKDF(ask, `luma-sync-member-v2`), account, device, sign_pub, box_pub, epoch) |
| Zone id | first 16 bytes, hex, of mac(HKDF(names, `luma-sync-zone-id-v2`), account, label) |
| Zone labels | `apps`, `keychain`, `appdata:<owner>:<app>`, `portal:<owner>:<app>`; owner `flatpak:<first 32 hex of SHA-256(lp(remote URL, signing-key fingerprints…))>` or `userland:<name>`; `local:<device>` never syncs |
| Record name | hex mac(HKDF(names, `luma-sync-record-name-v2`), zone, name) |
| Sealed object | format byte (2) ‖ nonce (24) ‖ XChaCha20-Poly1305 with AD = format byte ‖ the object's AD |
| Zone key wrap | sealed under HKDF(ask, `luma-sync-zone-wrap-v1`), AD lp(`luma-sync-zone-key-v2`, account, zone, generation, epoch) |
| Record | sealed zlib(canonical `{n, v, w, t}` or tombstone `{n, d:true, w, t}`; `w` writer device, `t` ms) under the zone key of its generation, AD lp(`luma-sync-record-v2`, account, zone, record name, generation, version); inflated size at most 4 MiB |
| Blob id | hex HMAC-SHA256(HKDF(zone key of the generation, `luma-sync-blob-id-v2\0<zone>`), plaintext) |
| Blob | generation (4 BE) ‖ sealed(length (4 BE) ‖ payload ‖ zero padding), AD lp(`luma-sync-blob-v2`, account, zone, blob id, generation); at most 4 MiB in all; no compression |
| Pieces | files over 1 MiB: fixed 1 MiB blocks, the last padded to 1 MiB, then random 1 MiB blocks up to Padmé(file size); smaller files share packs of at most 3.5 MiB, padded to Padmé(pack size); manifests in 1 MiB pieces padded to Padmé; uploads in random order |
| Padmé(L) | E = ⌊log2 L⌋, S = ⌊log2 E⌋ + 1, round L up to a multiple of 2^(E − S) |
| Join commitment | SHA-256(`luma-sync-join-commit-v1\0` ‖ device nonce) |
| Approvers' half | mac(HKDF(ask, `luma-sync-join-v2`), account, join id, device, commitment) |
| Join code | first 8 bytes BE of SHA-256(lp(`luma-sync-join-code-v2`, account, join id, device, sign_pub, box_pub, commitment, approvers' half, device nonce)) mod 10^6, 6 digits |
| Join rules | the joining device keeps the first approvers' half it sees and reveals once; the approver refuses a revealed nonce that doesn't open the commitment, checks the code and wraps the key from one and the same request row, and answers at most one open request per device and three new requests a day, each shown as a card; the joining device accepts an envelope only after the person confirms on it that both codes matched, and only from a member of the bundle, signed by it, whose approvers' half matches |
| Key fingerprint | first 16 bytes of SHA-256(sign_pub ‖ box_pub), hex in groups of 4 |

Reserved for bundle v3: `transport_pub` (the iroh transport key, certified by
the identity key).
