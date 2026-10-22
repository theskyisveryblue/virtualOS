import json
import http.server
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import brain


class OCRModelTests(unittest.TestCase):
    target = {'text':'Documents', 'x':.1, 'y':.7, 'width':.2, 'height':.1}

    def request(self, action):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({'message':{'content':json.dumps(action)}}).encode()
        return response

    def test_small_model_selects_only_a_detected_target_with_bounded_cpu_request(self):
        with patch.object(brain, '_http', return_value=self.request({'type':'click','ocr_index':0})) as http:
            action = brain._ollama_ocr_action([self.target], 'Open Documents', 'fixture-small', 'http://127.0.0.1:11434')
        self.assertEqual(action['type'], 'click')
        self.assertAlmostEqual(action['point_2d'][0], 200)
        self.assertAlmostEqual(action['point_2d'][1], 250)
        body = json.loads(http.call_args.args[2])
        self.assertNotIn('images', body['messages'][0])
        self.assertEqual(body['keep_alive'], 0)
        self.assertEqual(body['options']['num_gpu'], 0)
        self.assertEqual(body['options']['num_thread'], 2)
        self.assertEqual(body['options']['num_ctx'], 2048)
        self.assertEqual(body['options']['num_predict'], 64)

    def test_guessed_points_boolean_or_unknown_target_indexes_are_refused(self):
        for candidate in ({'type':'click','point_2d':[200,250]},
                          {'type':'click','ocr_index':True},
                          {'type':'click','ocr_index':1},
                          {'type':'click','ocr_index':-1},
                          {'type':'type','text':'unrequested text'}):
            with patch.object(brain, '_http', return_value=self.request(candidate)):
                action = brain._ollama_ocr_action([self.target], 'Open Documents', 'fixture-small', 'http://127.0.0.1:11434')
            self.assertEqual(action['type'], 'abstain')

    def test_no_usable_ocr_never_loads_a_model(self):
        for entries in ([], None, [dict(self.target, x=float('nan'))], [dict(self.target, width=2)]):
            with patch.object(brain, '_http') as http:
                action = brain._ollama_ocr_action(entries, 'Open Documents', 'fixture-small', 'http://127.0.0.1:11434')
            self.assertEqual(action['type'], 'abstain')
            http.assert_not_called()

    def test_model_reply_is_bounded_and_null_content_is_refused(self):
        for body in (b'x' * 65537, b'{"message":{"content":null}}'):
            response = MagicMock(); response.__enter__.return_value = response
            response.read.return_value = body
            with patch.object(brain, '_http', return_value=response), self.assertRaises(ValueError):
                brain._ollama_ocr_action([self.target], 'Open Documents', 'fixture-small', 'http://127.0.0.1:11434')
            response.read.assert_called_once_with(65537)


class FakeVM(brain.FleetVM):
    def __init__(self, status=200, blank=False, drift=False):
        super().__init__('vm-a')
        self.inputs = []
        self.status = status
        before = brain.Frame(b'before' * 20000 if not blank else b'black', 1501, 901)
        self.frames = [before] + ([] if drift else [before]) + [brain.Frame(b'after' * 20000, 1501, 901)]

    def health(self):
        return {'ready': True}

    def frame(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]

    def _input(self, body):
        self.inputs.append(body)
        return self.status


class BrainInputTests(unittest.TestCase):
    def test_ocr_runner_clicks_grounded_target_and_keeps_task_verification_separate(self):
        vm = FakeVM()
        response = OCRModelTests().request({'type':'click', 'ocr_index':0})
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name, \
             patch.object(vm, 'ocr', return_value=[OCRModelTests.target]), \
             patch.object(brain, '_http', return_value=response), \
             patch.object(brain, '_record'), patch.object(brain.time, 'sleep'):
            result = brain.run_brain(vm, 'Open Documents', 'fixture-small',
                'http://127.0.0.1:11434', 1, True, Path(name), perception='ocr')
        self.assertEqual(vm.inputs, [{'action':'click', 'nx':.2, 'ny':.25}])
        self.assertEqual(result.status, 'acted')
        self.assertFalse(result.to_json()['task_verified'])

    def test_shifted_text_is_never_silently_corrupted_by_legacy_supervisor(self):
        response = MagicMock(status=200)
        response.read.return_value = b'{"capability":{}}'
        response.__enter__.return_value = response
        with patch.object(brain, '_http', return_value=response) as network:
            vm = brain.FleetVM('vm-a')
            with self.assertRaises(ValueError): vm.type('chrome://version')
            with self.assertRaises(ValueError): vm.key('A')
        self.assertTrue(all(call.args[0] == 'GET' for call in network.call_args_list))

    def test_unicode_is_refused_without_any_guest_action(self):
        with patch.object(brain, '_http') as network, self.assertRaises(ValueError):
            brain.FleetVM('vm-a').type('private é')
        network.assert_not_called()

    def test_return_waits_for_legacy_scheduled_text_and_new_hosts_skip_delay(self):
        for headers, waits in (({}, [0.36]), ({'X-VOS-Input-Completed': '1'}, [])):
            events = []
            response = MagicMock(status=200, headers=headers)
            response.__enter__.return_value = response
            def send(method, url, body):
                events.append(json.loads(body)['action'])
                return response
            with patch.object(brain, '_http', side_effect=send), \
                 patch.object(brain.time, 'sleep', side_effect=lambda seconds: events.append(seconds)):
                vm = brain.FleetVM('vm-a')
                self.assertEqual(vm.type('typingproof12'), 200)
                self.assertEqual(vm.key('return'), 200)
            self.assertEqual(events, ['type'] + waits + ['key'])

    def test_overlong_text_is_refused_before_guest_input(self):
        with patch.object(brain, '_http') as send, self.assertRaises(ValueError):
            brain.FleetVM('vm-a').type('x' * 513)
        send.assert_not_called()

    def run_task(self, vm):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name, \
             patch.object(brain, '_ollama_action', return_value={'type': 'click', 'point_2d': [750, 250]}), \
             patch.object(brain.time, 'sleep'):
            return brain.run_brain(vm, 'synthetic target', 'fixture', 'http://127.0.0.1',
                                   1, True, Path(name))

    def test_model_point_is_normalized_before_guest_input(self):
        vm = FakeVM()
        result = self.run_task(vm)
        self.assertEqual(result.status, 'acted')
        self.assertEqual(vm.inputs, [{'action': 'click', 'nx': 0.75, 'ny': 0.25}])

    def test_guest_drift_during_model_inference_refuses_action(self):
        vm = FakeVM(drift=True)
        result = self.run_task(vm)
        self.assertEqual(result.status, 'stale_frame')
        self.assertEqual(vm.inputs, [])
        self.assertFalse(result.to_json()['task_verified'])

    def test_missing_pre_action_frame_refuses_action_without_crashing(self):
        vm = FakeVM()
        with patch.object(vm,'frame',side_effect=[vm.frames[0], urllib.error.URLError('fixture')]):
            result = self.run_task(vm)
        self.assertEqual(result.status,'frame_unavailable')
        self.assertEqual(vm.inputs,[])

    def test_health_and_initial_frame_failures_are_bounded_results(self):
        for stage, failure, expected in (
                ('health', ValueError('private fixture'), 'unavailable'),
                ('frame', TimeoutError('private fixture'), 'frame_unavailable')):
            vm = FakeVM()
            with patch.object(vm, stage, side_effect=failure), patch.object(brain, '_ollama_action') as model:
                result = self.run_task(vm)
            self.assertEqual(result.status, expected)
            self.assertEqual(result.error, type(failure).__name__)
            self.assertEqual(vm.inputs, [])

    def test_ocr_helper_and_model_timeouts_are_bounded_results_without_input(self):
        for helper_failure in (True, False):
            vm = FakeVM()
            with tempfile.TemporaryDirectory(dir='/private/tmp') as name, \
                 patch.object(vm, 'ocr', **({'side_effect':brain.subprocess.TimeoutExpired('helper', 1)}
                    if helper_failure else {'return_value':[OCRModelTests.target]})), \
                 patch.object(brain, '_ollama_ocr_action', side_effect=TimeoutError('private fixture')) as model:
                result = brain.run_brain(vm, 'fixture', 'fixture', 'http://127.0.0.1',
                                         1, True, Path(name), 'ocr')
            self.assertEqual(result.status, 'model_error')
            self.assertEqual(result.error, 'TimeoutExpired' if helper_failure else 'TimeoutError')
            self.assertEqual(vm.inputs, [])
            if helper_failure:
                model.assert_not_called()

    def test_pressure_before_second_request_preserves_prior_action_evidence(self):
        vm = FakeVM()
        vm.frames = [vm.frames[0]]
        check = MagicMock(side_effect=[None, brain.OCRAdmissionError('pressure changed')])
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name, \
             patch.object(vm, 'ocr', return_value=[OCRModelTests.target]), \
             patch.object(brain, '_ollama_ocr_action', return_value={'type':'click','point_2d':[200,250]}) as model, \
             patch.object(brain, '_record'), patch.object(brain.time, 'sleep'):
            result = brain.run_brain(vm, 'fixture', 'fixture', 'http://127.0.0.1',
                                     2, True, Path(name), 'ocr', check)
        self.assertEqual(result.status, 'resource_paused')
        self.assertEqual(result.steps, 2)
        self.assertEqual(len(vm.inputs), 1)
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0]['http'], 200)
        self.assertEqual(model.call_count, 1)
        self.assertFalse(result.to_json()['task_verified'])

    def test_input_connection_failure_is_uncertain_and_never_replayed(self):
        for failure in (TimeoutError('private fixture'), urllib.error.URLError('private fixture')):
            vm = brain.FleetVM('vm-a')
            frame = FakeVM().frames[0]
            with patch.object(vm, 'health', return_value={'ready':True}), \
                 patch.object(vm, 'frame', return_value=frame), \
                 patch.object(brain, '_http', side_effect=failure) as network:
                result = self.run_task(vm)
            self.assertEqual(result.status, 'input_uncertain')
            network.assert_called_once()
            self.assertIsNone(result.actions[0]['http'])
            self.assertIsNone(result.actions[0]['changed'])
            self.assertFalse(result.to_json()['task_verified'])

    def test_missing_after_frame_preserves_successful_input_without_claiming_change(self):
        vm = FakeVM()
        with patch.object(vm, 'frame', side_effect=[vm.frames[0], vm.frames[0],
                                                 TimeoutError('private fixture')]):
            result = self.run_task(vm)
        self.assertEqual(result.status, 'observation_unavailable')
        self.assertEqual(len(vm.inputs), 1)
        self.assertEqual(result.actions[0]['http'], 200)
        self.assertIsNone(result.actions[0]['changed'])
        self.assertFalse(result.to_json()['task_verified'])

    def test_trace_disk_failure_preserves_applied_action_and_stops(self):
        vm = FakeVM()
        with patch.object(brain, '_record', side_effect=OSError('disk full')):
            result = self.run_task(vm)
        self.assertEqual(result.status, 'trace_error')
        self.assertEqual(result.to_json()['trace_error'], 'OSError')
        self.assertEqual(len(vm.inputs), 1)
        self.assertEqual(result.actions[0]['http'], 200)
        self.assertTrue(result.actions[0]['changed'])
        self.assertFalse(result.to_json()['task_verified'])

    def test_pixel_edges_map_to_frame_edges(self):
        vm = FakeVM()
        image = vm.frames[0]
        vm.click(1500, 900, frame=image)
        self.assertEqual(vm.inputs[-1], {'action': 'click', 'nx': 1.0, 'ny': 1.0})

    def test_rejected_input_never_becomes_acted_when_frame_changes(self):
        result = self.run_task(FakeVM(status=503))
        self.assertEqual(result.status, 'input_failed')
        self.assertFalse(result.actions[0]['changed'])

    def test_blank_frame_does_not_reach_model(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name, \
             patch.object(brain, '_ollama_action') as model:
            result = brain.run_brain(FakeVM(blank=True), 'fixture', 'fixture',
                                    'http://127.0.0.1', 1, True, Path(name))
        self.assertEqual(result.status, 'blank_frame')
        model.assert_not_called()

    def test_rejected_machine_names_cannot_escape_trace_directory(self):
        for name in ('../vm-a', '..', '', 'vm-a/other'):
            with self.assertRaises(ValueError):
                brain.FleetVM(name)

    def test_paused_resource_guard_blocks_shadow_inference_too(self):
        args = brain.build_parser().parse_args(['run', '--task', 'fixture', '--vms', 'vm-a'])
        with patch.object(brain.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), \
             patch.object(brain, 'run_fleet') as fleet:
            self.assertEqual(brain.cmd_run(args), 75)
        fleet.assert_not_called()

    def test_ocr_mode_still_requires_resource_admission(self):
        args = brain.build_parser().parse_args(['run', '--task', 'fixture', '--vms', 'vm-a', '--perception', 'ocr'])
        with patch.object(brain.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), \
             patch.object(brain, 'run_fleet') as fleet:
            self.assertEqual(brain.cmd_run(args), 75)
        fleet.assert_not_called()

    def test_generic_guard_success_does_not_bypass_ocr_model_budget(self):
        args = brain.build_parser().parse_args(['run', '--task', 'fixture', '--vms', 'vm-a',
                                                '--perception', 'ocr', '--workers', '1'])
        with patch.object(brain.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
             patch.object(brain, 'admit_ocr', side_effect=brain.OCRAdmissionError('insufficient headroom')), \
             patch.object(brain, 'run_fleet') as fleet:
            self.assertEqual(brain.cmd_run(args), 75)
        fleet.assert_not_called()

    def test_ocr_inference_uses_checked_digest_instead_of_mutable_tag(self):
        args = brain.build_parser().parse_args(['run','--task','fixture','--vms','vm-a',
                                                '--perception','ocr','--workers','1'])
        admitted = MagicMock()
        check = MagicMock()
        admitted.__enter__.return_value = {'model_reference':'sha256-' + 'a'*64, 'recheck':check}
        with patch.object(brain.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
             patch.object(brain,'admit_ocr',return_value=admitted), \
             patch.object(brain,'run_fleet',return_value=[]) as fleet:
            self.assertEqual(brain.cmd_run(args),0)
        self.assertEqual(fleet.call_args.args[3], 'sha256-' + 'a'*64)
        self.assertIs(fleet.call_args.args[-1], check)


class OfflineTransportTests(unittest.TestCase):
    @staticmethod
    def jpeg(width=1500, height=900, marker=0xc0):
        def segment(code, payload):
            return bytes((255, code)) + (len(payload)+2).to_bytes(2, 'big') + payload
        sof = b'\x08' + height.to_bytes(2, 'big') + width.to_bytes(2, 'big') + b'\x01\x01\x11\x00'
        return (b'\xff\xd8' + segment(0xe0, b'fixture') + segment(marker, sof)
                + segment(0xda, b'\x01\x01\x00\x00\x3f\x00') + b'\x00\xff\xd9')

    def test_jpeg_headers_bind_actual_pixel_budget_to_http_dimensions(self):
        for marker in (0xc0, 0xc2):
            self.assertEqual(brain._jpeg_dimensions(self.jpeg(marker=marker)), (1500,900))
        for body in (self.jpeg(65535,65535), self.jpeg()[:-1], b'not JPEG',
                     b'\xff\xd8\xff\xe0\xff\xfftruncated\xff\xd9'):
            with self.assertRaises(ValueError):
                brain._jpeg_dimensions(body)
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {'X-Image-Width':'1000','X-Image-Height':'900'}
        response.read.return_value = self.jpeg()
        with patch.object(brain, '_http', return_value=response), self.assertRaises(ValueError):
            brain.FleetVM('vm-a').frame()

    def test_oversized_frame_or_invalid_dimensions_are_refused_with_bounded_reads(self):
        for width, height, body in (('8193','900',b''), ('4096','8192',b''),
                                   ('-1','900',b''), ('1500','900',b'x'*(brain.MAX_FRAME_BYTES+1))):
            response = MagicMock()
            response.__enter__.return_value = response
            response.headers = {'X-Image-Width':width, 'X-Image-Height':height}
            response.read.return_value = body
            with patch.object(brain, '_http', return_value=response), self.assertRaises(ValueError):
                brain.FleetVM('vm-a').frame()
            if body:
                response.read.assert_called_once_with(brain.MAX_FRAME_BYTES+1)
            else:
                response.read.assert_not_called()

    def test_fleet_health_rejects_oversized_or_malformed_json(self):
        for data in (b'x'*1048577, b'[]', b'{"instances":[null]}'):
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = data
            with patch.object(brain, '_http', return_value=response), self.assertRaises(ValueError):
                brain.FleetVM('vm-a').health()
            response.read.assert_called_once_with(1048577)

    def test_remote_or_credential_bearing_urls_are_refused_before_network(self):
        with patch.object(brain.urllib.request, 'build_opener') as network:
            for url in ('https://example.com/api/chat', 'http://192.168.1.2:9225',
                        'file:///etc/passwd', 'http://user:password@127.0.0.1:9225',
                        'http://127.0.0.1:bad', 'http://127.0.0.1#fragment'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    brain._http('POST', url, b'private-fixture')
            network.assert_not_called()

    def test_localhost_is_pinned_and_ipv6_loopback_remains_supported(self):
        self.assertEqual(brain._loopback_url('http://localhost:9225/fleet/status'),
                         'http://127.0.0.1:9225/fleet/status')
        self.assertEqual(brain._loopback_url('http://[::1]:9225'), 'http://[::1]:9225')

    def test_redirects_and_ambient_proxies_cannot_receive_guest_payloads(self):
        observed = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                observed.append(self.path)
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/outside')
                self.end_headers()
            def do_GET(self):
                observed.append(self.path)
                self.send_response(200); self.end_headers()
            def log_message(self, *args):
                pass
        with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}), \
                     self.assertRaises(urllib.error.HTTPError) as caught:
                    brain._http('POST', f'http://127.0.0.1:{server.server_port}/redirect', b'private-fixture')
                self.assertEqual(caught.exception.code, 302)
                self.assertEqual(observed, ['/redirect'])
            finally:
                server.shutdown()
                thread.join(timeout=2)


class TraceStorageTests(unittest.TestCase):
    def record(self, directory):
        brain._record(directory, 'vm-a', 1, 'private fixture task', 'click',
                      [1,2], 'a'*64, 'b'*64, True)

    def test_new_trace_is_private_and_stores_task_hash_only(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            directory = Path(name)/'vm-a'
            self.record(directory)
            path = directory/'brain.jsonl'
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertNotIn('private fixture task', path.read_text())
            self.assertEqual(json.loads(path.read_text())['action'], 'click')

    def test_linked_trace_files_cannot_modify_existing_targets(self):
        for hardlink in (False, True):
            with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
                directory = Path(name)/'vm-a'; directory.mkdir(mode=0o700)
                target = Path(name)/'preserved'; target.write_text('unchanged'); target.chmod(0o600)
                if hardlink:
                    os.link(target, directory/'brain.jsonl')
                else:
                    (directory/'brain.jsonl').symlink_to(target)
                with self.assertRaises(OSError):
                    self.record(directory)
                self.assertEqual(target.read_text(), 'unchanged')

    def test_fifo_or_shared_trace_is_refused_without_blocking(self):
        for fifo in (False, True):
            with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
                directory = Path(name)/'vm-a'; directory.mkdir(mode=0o700)
                path = directory/'brain.jsonl'
                if fifo:
                    os.mkfifo(path, 0o600)
                else:
                    path.write_text('unchanged'); path.chmod(0o644)
                with self.assertRaises(OSError):
                    self.record(directory)


if __name__ == '__main__':
    unittest.main()
