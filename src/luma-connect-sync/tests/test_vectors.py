# SPDX-License-Identifier: Apache-2.0
"""Frozen wire vectors, format 2 (docs/operations/connect-sync-wire.md). A change here changes ids or keys on every device."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from luma_connect_sync import crypto  # noqa: E402

NAMES, ASK = bytes(range(32)), bytes(range(32, 64))


class Vectors(unittest.TestCase):
    def test_vectors(self) -> None:
        recovery = crypto.recovery_keys(bytes(32), "acct-vector")
        self.assertEqual(recovery.box_pub.hex(), "5941fdbd613a3bdaffd58be905fecc4124ac28356e083c1027a536c783ed6f30")
        self.assertEqual(recovery.auth_pub.hex(), "2424931bbae302926914b3098364f9d481698d1faa567fd71d25ec42085c3f79")
        bundle = crypto.Bundle("acct-vector", 1, ASK, NAMES, recovery.box_pub, recovery.auth_pub, {})
        zone = bundle.zone_id("apps")
        self.assertEqual(crypto.FORMAT, 2)
        self.assertEqual(zone, "16c9216f5dc11edbbbe96ee469ee161d")
        self.assertEqual(crypto.record_name(NAMES, zone, "flatpak:org.gnome.TextEditor"), "1c20cd601e781ad96ea4583f1f2ea51bfbf76c27bda197b41845826d1076e90b")
        self.assertEqual(crypto.blob_id(bytes(range(64, 96)), zone, b"hello"), "91cafe89d793d11c50a01904f7885e393cc53f5f2d1f439aef1d205f6333bb79")
        self.assertEqual(bundle.join_nonce("J" * 22, "dev", bytes(32)).hex(), "eabcaed811426dbbb6a346dcde81cfec8db4cdde9469d18ba74e8d44d59641d9")
        self.assertEqual(crypto.join_code("acct-vector", "J" * 22, "dev", bytes(32), bytes(32), bytes(32), bytes(32), bytes(range(32))), "580704")
        self.assertEqual(crypto.join_commitment(bytes(32)).hex(), "9e5eaa0543b718c1f3978e29758fbffa0f283bb3b1a25f73b8b9d78ecfa33a03")
        self.assertEqual(bundle.cert("dev", bytes(32), bytes(32)).hex(), "71b6018447f7a7853f4edd1746bdf04cd5628fc196a8f7e948c017dcb8158cb1")
        self.assertEqual(bundle._wrap_key().hex(), "8c5b74c6d9ec7cb3c1a0a1de41746143cec4e20f8d8141b9280d8118f7746885")
        self.assertEqual(crypto.lp("ab", 3, b"\x00").hex(), "00000002616200000001330000000100")
        self.assertEqual([crypto.padme(n) for n in (0, 1, 100, 1000, 4096, 1 << 20, (1 << 20) + 1, 5 * (1 << 20) + 7)],
                         [0, 1, 104, 1024, 4096, 1048576, 1081344, 5373952])

    def test_recovery_words_are_bip39(self) -> None:
        self.assertEqual(crypto.recovery_words(bytes(32)), ["abandon"] * 23 + ["art"])
        self.assertEqual(crypto.recovery_words(b"\xff" * 32), ["zoo"] * 23 + ["vote"])


if __name__ == "__main__":
    unittest.main()
