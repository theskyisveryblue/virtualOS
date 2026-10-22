import copy
import json
from pathlib import Path
import unittest

from linux_evidence import describe


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[1] / 'experiments'
        self.row = json.loads((directory / 'linux-maintained-persistence-receipt-20261018.json').read_text())

    def test_real_maintained_receipt_is_explicitly_historical_and_isolated(self):
        message = describe(self.row, maintained=True)
        self.assertIn('VERIFIED isolated 6.18.57-0-virt', message)
        self.assertIn('historical receipt', message)

    def test_missing_boot_signature_or_locked_root_never_verifies(self):
        for key in ('disk_rooted_boot_2', 'all_package_signatures_verified', 'root_login_locked'):
            row = copy.deepcopy(self.row); row[key] = False
            with self.assertRaises(ValueError):
                describe(row, maintained=True)

    def test_boolean_marker_count_and_changed_disk_or_profile_are_refused(self):
        mutations = (
            ('persist_marker_counts', [True, 2]),
            ('kernel_sha256', '0' * 64),
            ('legacy_regular_userland_files_retained', 1))
        for key, value in mutations:
            row = copy.deepcopy(self.row); row[key] = value
            with self.assertRaises(ValueError):
                describe(row, maintained=True)
        self.row['files']['root.img']['sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            describe(self.row, maintained=True)


if __name__ == '__main__':
    unittest.main()
