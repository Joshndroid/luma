# SPDX-License-Identifier: Apache-2.0
"""XChaCha20-Poly1305 (IETF) from libsodium, the one primitive ADR-054 names
that python-cryptography does not carry. Nothing is implemented here: the
two calls go straight to libsodium's ``crypto_aead_xchacha20poly1305_ietf_*``.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os

KEY_BYTES = 32
NONCE_BYTES = 24
TAG_BYTES = 16


class SodiumError(Exception):
    pass


def _load() -> ctypes.CDLL:
    names = [os.environ.get("LUMA_SODIUM_LIBRARY") or "", ctypes.util.find_library("sodium") or "",
             "libsodium.so.26", "libsodium.so.23", "libsodium.dylib"]
    for name in names:
        if not name:
            continue
        try:
            library = ctypes.CDLL(name)
        except OSError:
            continue
        if library.sodium_init() < 0:
            raise SodiumError("libsodium could not start.")
        return library
    raise SodiumError("libsodium is not installed.")


_lib = None


def _sodium() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        _lib = _load()
        ull, out_len, data = ctypes.c_ulonglong, ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_char_p
        encrypt = _lib.crypto_aead_xchacha20poly1305_ietf_encrypt     # (c, clen_p, m, mlen, ad, adlen, nsec, npub, k)
        encrypt.restype, encrypt.argtypes = ctypes.c_int, [data, out_len, data, ull, data, ull, ctypes.c_void_p, data, data]
        decrypt = _lib.crypto_aead_xchacha20poly1305_ietf_decrypt     # (m, mlen_p, nsec, c, clen, ad, adlen, npub, k)
        decrypt.restype, decrypt.argtypes = ctypes.c_int, [data, out_len, ctypes.c_void_p, data, ull, data, ull, data, data]
        if _lib.crypto_aead_xchacha20poly1305_ietf_npubbytes() != NONCE_BYTES or _lib.crypto_aead_xchacha20poly1305_ietf_keybytes() != KEY_BYTES:
            raise SodiumError("libsodium reports unexpected XChaCha20-Poly1305 sizes.")
    return _lib


def encrypt(key: bytes, nonce: bytes, plaintext: bytes, associated: bytes) -> bytes:
    if len(key) != KEY_BYTES or len(nonce) != NONCE_BYTES:
        raise ValueError("bad key or nonce length")
    out = ctypes.create_string_buffer(len(plaintext) + TAG_BYTES)
    length = ctypes.c_ulonglong(0)
    if _sodium().crypto_aead_xchacha20poly1305_ietf_encrypt(out, ctypes.byref(length), plaintext, len(plaintext),
                                                             associated, len(associated), None, nonce, key) != 0:
        raise SodiumError("encryption failed")
    return out.raw[:length.value]


def decrypt(key: bytes, nonce: bytes, ciphertext: bytes, associated: bytes) -> bytes:
    if len(key) != KEY_BYTES or len(nonce) != NONCE_BYTES:
        raise ValueError("bad key or nonce length")
    if len(ciphertext) < TAG_BYTES:
        raise SodiumError("ciphertext too short")
    out = ctypes.create_string_buffer(max(1, len(ciphertext) - TAG_BYTES))
    length = ctypes.c_ulonglong(0)
    if _sodium().crypto_aead_xchacha20poly1305_ietf_decrypt(out, ctypes.byref(length), None, ciphertext, len(ciphertext),
                                                             associated, len(associated), nonce, key) != 0:
        raise SodiumError("decryption failed: wrong key or altered data")
    return out.raw[:length.value]
