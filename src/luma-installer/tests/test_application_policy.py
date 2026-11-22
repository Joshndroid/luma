# SPDX-License-Identifier: Apache-2.0
"""The application domain and the person's folders (data/selinux).

Every standard folder bound into a capsule or a store application's sandbox
(launcher._person_folders) must be usable with the same rights, whatever type
the policy gives it; nothing else in the home folder gets a rule.
"""
import re
import unittest
from pathlib import Path

CIL = Path(__file__).resolve().parents[1] / "data/selinux/luma_application.cil"
# Fedora's types for Desktop, Documents, Downloads, Music, Pictures, Public,
# Templates and Videos: Music is audio_home_t, the others user_home_t.
FOLDER_TYPES = ("user_home_t", "audio_home_t")
CLASSES = ("dir", "file", "lnk_file")
# Keys, keyrings, certificates, browser profiles and the home folder itself.
PRIVATE_TYPES = {
    "user_home_dir_t", "ssh_home_t", "gpg_secret_t", "gpg_agent_tmp_t", "home_cert_t",
    "gkeyringd_gnome_home_t", "gnome_home_t", "config_home_t", "cache_home_t", "data_home_t",
    "mozilla_home_t", "chrome_sandbox_home_t", "user_home_type", "home_type", "file_type",
    "non_security_file_type", "non_auth_file_type",
}
RULE = re.compile(r"^\s*\((allow|dontaudit|deny|auditallow) process (\S+) \((\w+) \(([^)]*)\)\)\)")
RANGE = re.compile(r"^\s*\(rangetransition process (\S+) (\w+) \(\(s0\) \(s0\)\)\)")


def rules():
    text = "\n".join(line for line in CIL.read_text().splitlines() if not line.lstrip().startswith(";"))
    allows, ranges = {}, set()
    for line in text.splitlines():
        if match := RULE.match(line):
            kind, target, cls, perms = match.groups()
            allows.setdefault((kind, target, cls), set()).update(perms.split())
        elif match := RANGE.match(line):
            ranges.add(match.groups())
    return allows, ranges


class PersonFolders(unittest.TestCase):
    def test_every_folder_type_has_the_same_rights(self):
        allows, ranges = rules()
        for cls in CLASSES:
            expected = allows[("allow", "user_home_t", cls)]
            for folder_type in FOLDER_TYPES:
                with self.subTest(folder_type=folder_type, cls=cls):
                    self.assertEqual(allows.get(("allow", folder_type, cls)), expected)
                    self.assertIn((folder_type, cls), ranges)

    def test_folders_are_listed_read_and_written(self):
        allows, _ = rules()
        for folder_type in FOLDER_TYPES:
            self.assertLessEqual({"getattr", "search", "open", "read", "add_name", "write"},
                                 allows[("allow", folder_type, "dir")])
            self.assertLessEqual({"getattr", "open", "read", "write", "create"},
                                 allows[("allow", folder_type, "file")])

    def test_no_other_home_type_is_reached(self):
        allows, ranges = rules()
        targets = {target for kind, target, _ in allows if kind == "allow"} | {target for target, _ in ranges}
        self.assertFalse(targets & PRIVATE_TYPES)
        home = {target for target in targets if "home" in target}
        self.assertEqual(home, set(FOLDER_TYPES))


if __name__ == "__main__":
    unittest.main()
