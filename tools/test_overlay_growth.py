import tempfile
import json
from pathlib import Path
import unittest

from overlay_growth import BLOCK, LIMIT, read_samples, summarize


def sample(stamp, blocks=10, codec='raw'):
    return dict(observed_at_unix=stamp, changed_blocks=blocks,
                logical_changed_bytes=blocks * BLOCK, logical_limit_bytes=LIMIT,
                database_bytes=1000000, allocated_bytes=1000000, wal_bytes=100,
                codec=codec, read_only=True)


class GrowthTests(unittest.TestCase):
    def test_physical_reserve_is_independent_of_remaining_logical_capacity(self):
        row = sample(100)
        row['volume_available_bytes'] = 7 * 1024**3
        result = summarize([row], now=110)
        self.assertFalse(result['host_volume']['host_reserve_reference_met'])
        self.assertFalse(result['host_volume']['vm_start_disk_reference_met'])
        self.assertEqual(result['host_volume']['bytes_short_of_vm_start_disk_reference'], 7 * 1024**3)
        row['volume_available_bytes'] = 16 * 1024**3
        result = summarize([row], now=1000)
        self.assertEqual(result['host_volume']['state'], 'stale')
        self.assertIsNone(result['host_volume']['host_reserve_reference_met'])
        self.assertIsNone(result['host_volume']['vm_start_disk_reference_met'])
        self.assertTrue(result['host_volume']['reference_thresholds_do_not_replace_live_resource_admission'])

    def test_contiguous_growth_and_projection_use_logical_cap(self):
        rows = [sample(stamp, 10 + stamp // 100) for stamp in range(100, 701, 100)]
        result = summarize(rows, now=710)
        self.assertEqual(result['state'], 'fresh')
        self.assertEqual(result['logical_growth_bytes_per_hour'], BLOCK * 36)
        self.assertEqual(result['allocated_growth_bytes_per_hour'], 0)
        self.assertAlmostEqual(result['projected_hours_to_logical_limit'], (LIMIT-17*BLOCK)/(BLOCK*36))

    def test_outage_is_reported_and_not_used_for_forecast(self):
        result = summarize([sample(100), sample(200, 20), sample(1000, 300)], now=1010)
        self.assertEqual(result['gap_count'], 1)
        self.assertEqual(result['largest_gap_seconds'], 800)
        self.assertEqual(result['recent_contiguous_seconds'], 0)
        self.assertIsNone(result['logical_growth_bytes_per_hour'])
        self.assertIsNone(result['projected_hours_to_logical_limit'])

    def test_stale_codec_change_and_pruning_do_not_make_false_forecasts(self):
        self.assertEqual(summarize([sample(100)], now=1000)['state'], 'stale')
        result = summarize([sample(100), sample(200, codec='zlib-v1')], now=210)
        self.assertEqual(result['recent_contiguous_seconds'], 0)
        result = summarize([sample(100, 20), sample(200, 10)], now=210)
        self.assertLess(result['logical_growth_bytes_per_hour'], 0)
        self.assertIsNone(result['projected_hours_to_logical_limit'])

    def test_invalid_samples_and_duplicates_cannot_inflate_history(self):
        bad = sample(100); bad['changed_blocks'] = True
        result = summarize([None, bad, sample(float('nan')), sample(100), sample(100), sample(200)], now=210)
        self.assertEqual(result['rejected_samples'], 3)
        self.assertEqual(result['sample_count'], 2)

    def test_log_tail_is_bounded_and_torn_records_are_ignored(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'observer.log'
            line = 'write_overlay_snapshot=' + json.dumps(sample(100)) + '\n'
            path.write_text('x' * 4000 + '\n' + line + 'write_overlay_snapshot={broken\n')
            before = path.read_bytes()
            self.assertEqual(read_samples(path, maximum=1024), [sample(100)])
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
