import os
import tempfile
import unittest
from pathlib import Path

import ocswarm


class SnapshotTests(unittest.TestCase):
    def test_equal_size_and_timestamp_do_not_hide_a_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.txt"
            snapshot = root / "snapshot.txt"
            source.write_bytes(b"old\n")
            snapshot.write_bytes(b"new\n")
            stamp = source.stat().st_mtime_ns
            os.utime(snapshot, ns=(stamp, stamp))
            self.assertEqual(source.stat().st_size, snapshot.stat().st_size)
            self.assertEqual(source.stat().st_mtime_ns, snapshot.stat().st_mtime_ns)
            self.assertFalse(ocswarm.files_same(source, snapshot))
            snapshot.write_bytes(b"old\n")
            self.assertTrue(ocswarm.files_same(source, snapshot))


if __name__ == "__main__":
    unittest.main()
