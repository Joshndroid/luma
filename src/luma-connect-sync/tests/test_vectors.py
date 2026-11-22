# SPDX-License-Identifier: Apache-2.0
"""Frozen wire vectors (docs/operations/connect-sync-wire.md). A change here changes ids or keys on every device."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from luma_connect_sync import chunking, crypto  # noqa: E402

NAMES, ASK = bytes(range(32)), bytes(range(32, 64))


class Vectors(unittest.TestCase):
    def test_vectors(self) -> None:
        bundle = crypto.Bundle("acct-vector", 1, ASK, NAMES, bytes(32), {})
        zone = bundle.zone_id("apps")
        self.assertEqual(zone, "cc43b200902f0f924cabb928819cfb19")
        self.assertEqual(crypto.record_name(NAMES, zone, "flatpak:org.gnome.TextEditor"), "5fca695e1bbc775ac3185f788dc1e78d4af7de9c70c3920ecc95a55e0b190261")
        self.assertEqual(crypto.blob_id(NAMES, zone, b"hello"), "c6bc5f02192d440ac8517fd949638491b28536c2dbb7fa4a127a8f5f0f1778ab")
        self.assertEqual(bundle.join_nonce("J" * 22, "dev", bytes(32)).hex(), "e258a48790c6ab41a167ae3a52b572e1c7c237ac089df53cae78606e2eec3e0f")
        self.assertEqual(crypto.join_code("acct-vector", "dev", bytes(32), bytes(32), bytes(32), bytes(range(32))), "752196")
        self.assertEqual(crypto.join_commitment(bytes(32)).hex(), "9e5eaa0543b718c1f3978e29758fbffa0f283bb3b1a25f73b8b9d78ecfa33a03")
        self.assertEqual(bundle.cert("dev", bytes(32), bytes(32)).hex(), "5eb3e4493fa639c2c0ae8321e402bc7e2923fa569b359506d6929bd7187ed76a")
        self.assertEqual(crypto.hkdf(NAMES, "luma-sync-chunk-v1\0" + zone).hex(), "9ed12aa09cd155d60c6d1efdeb816a0b9651aac2d91a64f1a89cdbe57a4b3015")
        self.assertEqual(chunking.gear(bytes(32))[0], 0x10e6878212a63cf1)

    def test_recovery_words_are_bip39(self) -> None:
        self.assertEqual(crypto.recovery_words(bytes(32)), ["abandon"] * 23 + ["art"])
        self.assertEqual(crypto.recovery_words(b"\xff" * 32), ["zoo"] * 23 + ["vote"])
        key = crypto.recovery_keypair(bytes(32), "acct-vector").public_key().public_bytes(**crypto.RAW)
        self.assertEqual(key.hex(), "5941fdbd613a3bdaffd58be905fecc4124ac28356e083c1027a536c783ed6f30")


if __name__ == "__main__":
    unittest.main()
