"""Basic package import smoke test."""

from __future__ import annotations

import unittest

import src_py_lib


class PackageImportTest(unittest.TestCase):
    """Verify the package can be imported."""

    def test_package_imports(self) -> None:
        self.assertIsNotNone(src_py_lib)


if __name__ == "__main__":
    unittest.main()
