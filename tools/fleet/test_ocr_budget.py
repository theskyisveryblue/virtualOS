from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from ocr_budget import admit, GIB, OCRAdmissionError


class OCRBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'lock'
        self.model = {'name':'small:latest','size':400*1024**2,'digest':'a'*64,'details':{'format':'gguf'}}
        self.row = dict(available_bytes=7*GIB, disk_free_bytes=12*GIB,
                        cpus=8, load=2., swap_used_bytes=GIB, swapouts=100)
        self.fetch = Mock(side_effect=lambda url:
            {'models':[self.model]} if url.endswith('/api/tags') else {'models':[]})
        self.probe = Mock(return_value=self.row)

    def admitted(self, **changes):
        options = dict(model='small',endpoint='http://127.0.0.1:11434',workers=1,vm_count=1,
                       fetch=self.fetch,probe=self.probe,pause=lambda _:None,directory=self.directory)
        options.update(changes)
        return admit(**options)

    def test_small_installed_model_with_idle_headroom_can_hold_one_session(self):
        with self.admitted() as result:
            self.assertEqual(result['model_bytes'],400*1024**2)
            self.assertEqual(result['minimum_available_bytes'],6*GIB)
            self.assertEqual(result['model_reference'],'sha256-' + 'a'*64)
            self.assertEqual(result['model_sha256'],'a'*64)
            with self.assertRaises(OCRAdmissionError):
                with self.admitted(): pass
        with self.admitted(): pass

    def test_large_unknown_or_ambiguous_model_never_admits(self):
        for change in ({'size':4*GIB},{'size':True},{'name':'other:latest'}, {'digest':'unknown'}):
            original = dict(self.model); self.model.update(change)
            with self.assertRaises(OCRAdmissionError):
                with self.admitted(): pass
            self.model = original

    def test_active_model_and_mid_check_race_are_refused(self):
        for answers in (({'models':[self.model]}, {'models':[{'name':'other'}]}),
                        ({'models':[self.model]}, {'models':[]}, {'models':[{'name':'other'}]})):
            with self.assertRaises(OCRAdmissionError):
                with self.admitted(fetch=Mock(side_effect=answers)): pass

    def test_cloud_routes_and_unknown_weight_format_are_refused_before_guest_data(self):
        for mutation in ({'remote_host':'https://remote.invalid'}, {'remote_model':'cloud-model'},
                         {'details':{}}, {'remote_host':None}):
            original = dict(self.model); self.model.update(mutation)
            with self.assertRaises(OCRAdmissionError):
                with self.admitted(): pass
            self.assertEqual(self.probe.call_count,0)
            self.model = original

    def test_available_ram_swap_activity_cpu_disk_and_swap_allocation_gate(self):
        for mutation in ({'available_bytes':5*GIB}, {'load':8.},
                         {'disk_free_bytes':7*GIB}, {'swap_used_bytes':8*GIB},
                         {'swapouts':101}, {'available_bytes':True}):
            after = dict(self.row, **mutation)
            with self.assertRaises(OCRAdmissionError):
                with self.admitted(probe=Mock(side_effect=[self.row,after])): pass

    def test_multiple_workers_or_vms_are_refused_before_metadata(self):
        for workers, count in ((2,1),(1,2),(True,1)):
            self.fetch.reset_mock()
            with self.assertRaises(OCRAdmissionError):
                with self.admitted(workers=workers,vm_count=count): pass
            self.fetch.assert_not_called()

    def test_symlink_lock_directory_is_refused(self):
        self.directory.symlink_to(self.directory.parent)
        with self.assertRaises(OCRAdmissionError):
            with self.admitted(): pass
        self.fetch.assert_not_called()

    def test_body_failure_is_not_misreported_as_pre_inference_refusal(self):
        with self.assertRaisesRegex(RuntimeError,'after inference'):
            with self.admitted():
                raise RuntimeError('after inference')

    def test_later_requests_recheck_pressure_and_unrelated_loaded_models(self):
        with self.admitted() as budget:
            budget['recheck']()
            self.probe.return_value = dict(self.row, available_bytes=5*GIB)
            with self.assertRaises(OCRAdmissionError):
                budget['recheck']()
            self.probe.return_value = self.row
            self.fetch.side_effect = lambda _: {'models':[{'name':'another-model'}]}
            with self.assertRaises(OCRAdmissionError):
                budget['recheck']()

    def test_request_recheck_refuses_swapouts_or_model_load_during_sampling(self):
        with self.admitted() as budget:
            self.probe.side_effect = [self.row, dict(self.row, swapouts=101)]
            with self.assertRaises(OCRAdmissionError):
                budget['recheck']()
            self.probe.side_effect = None
            self.fetch.side_effect = [{'models':[]}, {'models':[{'name':'another-model'}]}]
            with self.assertRaises(OCRAdmissionError):
                budget['recheck']()


if __name__ == '__main__':
    unittest.main()
