import unittest

from demos.install import codex_asset


class CodexAssetTests(unittest.TestCase):
    def test_codex_asset(self):
        for system, machine, name in [
            ("Darwin", "arm64", "codex-aarch64-apple-darwin"),
            ("Darwin", "x86_64", "codex-x86_64-apple-darwin"),
            ("Linux", "x86_64", "codex-x86_64-unknown-linux-musl"),
            ("Linux", "aarch64", "codex-aarch64-unknown-linux-musl"),
        ]:
            with self.subTest(system=system, machine=machine):
                self.assertEqual(codex_asset(system, machine), name)
        with self.assertRaises(RuntimeError):
            codex_asset("Windows", "AMD64")


if __name__ == "__main__":
    unittest.main()
