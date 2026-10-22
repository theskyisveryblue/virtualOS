"""Describe isolated historical Linux proof receipts, without claiming rollout."""
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from linux_guest_selection import PINS, DISK_SHA256, DISK_BYTES, read_json


def describe(row, maintained=False):
    if not isinstance(row, dict):
        raise ValueError('receipt must be an object')
    schema = ('virtualos-maintained-linux-persistence-receipt/v1' if maintained else
              'virtualos-linux-persistence-receipt/v1')
    if row.get('schema') != schema:
        raise ValueError('unexpected proof schema')
    for key in ('root_login_locked', 'disk_rooted_boot_1', 'disk_rooted_boot_2',
                'persistent', 'proof_disk_retained', 'logs_preserved'):
        if row.get(key) is not True:
            raise ValueError('incomplete persistence proof')
    counts = row.get('persist_marker_counts')
    if (not isinstance(counts, list) or len(counts) != 2
            or any(type(value) is not int for value in counts) or counts != [1, 2]
            or row.get('root_shadow_mode') != '0600'
            or row.get('root_mount_verified') != 'ext4,rw'
            or row.get('production_guests_restarted_by_lab') is not False
            or row.get('source_disk_formatted') is not False):
        raise ValueError('incomplete isolation or persistence evidence')
    disk = row.get('files', {}).get('root.img', {})
    if (not isinstance(disk, dict) or type(disk.get('bytes')) is not int
            or disk['bytes'] != DISK_BYTES or not isinstance(disk.get('sha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', disk['sha256'])):
        raise ValueError('proof disk identity is missing')
    kernel = row.get('kernel_release')
    if not isinstance(kernel, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-0-virt', kernel):
        raise ValueError('kernel release missing')
    if maintained:
        if (any(row.get(key) != value for key, value in PINS.items())
                or disk['sha256'] != DISK_SHA256
                or row.get('maintained_userland') is not True
                or row.get('all_package_signatures_verified') is not True
                or type(row.get('legacy_regular_userland_files_retained')) is not int
                or row['legacy_regular_userland_files_retained'] != 0):
            raise ValueError('maintained artifact or package evidence does not match')
    elif row.get('kernel_package_signature_verified') is not True:
        raise ValueError('kernel signature proof missing')
    return 'VERIFIED isolated %s; two boots; locked root (historical receipt)' % kernel


if __name__ == '__main__':
    root = Path(sys.argv[1]).resolve() / 'tools/experiments'
    for name, label, maintained in (
            ('linux-persistence-receipt-20261018.json', 'Linux disk persistence', False),
            ('linux-maintained-persistence-receipt-20261018.json', 'Maintained Linux proof', True)):
        try:
            message = describe(read_json(root / name, private=False), maintained)
        except (OSError, ValueError, TypeError, AttributeError):
            message = 'no complete matching historical proof'
        print('%-26s %s' % (label, message))
    print('%-26s %s' % ('Linux default rollout', 'not verified by isolated proof receipts'))
