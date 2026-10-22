import io
import http.client
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request

import cloud_disk_archive_delegate as delegate
from cloud_disk import digest
from cloud_disk_archive_session import identity
from cloud_disk_r2_upload import R2RetryableError, R2UploadStore
from test_cloud_disk_archive import fixture


class RetryTests(unittest.TestCase):
    def test_worker_retains_chunk_and_requests_fresh_signatures_after_transport_loss(self):
        raw_disk = b'disk' * 128
        archive = fixture([('./Disk.img', raw_disk)])
        config = json.loads((Path(__file__).parent / 'cloudflare/cloud-disk.ai.json').read_text())
        pins = dict(archive_bytes=len(archive), archive_sha256=digest(archive),
                    disk_bytes=len(raw_disk), disk_sha256=digest(raw_disk))
        channel = Mock()
        rows = []
        def exchange(row):
            rows.append(row['request'])
            return {'id': row['request']['id'], 'headers': {'If-None-Match': '*'}}
        channel.exchange.side_effect = exchange
        objects, attempts, put_attempts = {}, [], []
        def network(request, limit):
            attempts.append(request)
            key = request.full_url.split(config['object_prefix'] + '/')[1]
            if request.get_method() == 'PUT':
                put_attempts.append(request)
                if len(put_attempts) <= 2:
                    # The object was written, but its response never reached us.
                    objects.setdefault(key, request.data)
                    raise R2RetryableError('connection lost after bounded PUT')
                if key in objects:
                    return 412, b''
                objects[key] = request.data
                return 200, b''
            return (200, objects[key]) if key in objects else (404, b'')
        original_retry = delegate.retry_r2
        fast_retry = lambda operation, phase: original_retry(operation, phase=phase, pause=lambda _: None)
        with patch.dict(os.environ), \
             patch('cloud_disk_archive_session.ArchiveSource', return_value=io.BytesIO(archive)), \
             patch.object(R2UploadStore, '_persistent_network', side_effect=network), \
             patch.object(delegate, 'retry_r2', side_effect=fast_retry):
            result = delegate.worker(dict(config=config, pins=pins, operation='convert', source_url='private'), channel)
        self.assertTrue(result['readback_verified'])
        self.assertEqual(result['disk_sha256'], pins['disk_sha256'])
        self.assertGreater(result['reused_bytes'], 0)
        self.assertEqual([row['id'] for row in rows], list(range(1, len(rows) + 1)))
        self.assertEqual([request.data for request in put_attempts[:3]], [put_attempts[0].data] * 3)
        self.assertEqual(sum(call.args[0]['state'] == 'retrying_r2'
                             for call in channel.phase.call_args_list), 2)
        self.assertEqual(channel.phase.call_args.args[0]['verified_bytes'], len(raw_disk))
        self.assertIn('manifest.json', objects)

    def test_retry_budget_is_bounded_and_integrity_errors_are_immediate(self):
        operation = Mock(side_effect=R2RetryableError('temporary'))
        pauses = []
        with self.assertRaises(R2RetryableError):
            delegate.retry_r2(operation, pause=pauses.append, attempts=3)
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(pauses, [1, 2])
        operation = Mock(side_effect=ValueError('hash mismatch'))
        with self.assertRaises(ValueError):
            delegate.retry_r2(operation, pause=pauses.append)
        self.assertEqual(operation.call_count, 1)

    def test_retryable_http_statuses_do_not_include_authentication_failures(self):
        for status, expected in ((503, R2RetryableError), (429, R2RetryableError), (403, OSError)):
            response = SimpleNamespace(status=status, read=lambda limit: b'', will_close=True)
            factory = Mock(return_value=SimpleNamespace(request=lambda *a, **k: None,
                getresponse=lambda: response, close=lambda: None))
            with self.subTest(status=status), self.assertRaises(expected) as caught:
                R2UploadStore._persistent_network(Request('https://r2.invalid/object'), 1024,
                                                  connection_factory=factory)
            if status == 403:
                self.assertNotIsInstance(caught.exception, R2RetryableError)

    def test_lost_connections_are_retryable_without_exposing_request_details(self):
        def factory(*args, **kwargs):
            return SimpleNamespace(request=Mock(side_effect=ConnectionResetError('private capability')),
                                   close=lambda: None)
        with self.assertRaises(R2RetryableError) as caught:
            R2UploadStore._persistent_network(Request('https://r2.invalid/object'), 1024,
                                              connection_factory=factory)
        self.assertNotIn('private capability', str(caught.exception))

    def test_truncated_content_length_get_retries_the_connection(self):
        replies = iter((b'abc', b'abcdef'))
        def factory(*args, **kwargs):
            wire = b'HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n' + next(replies)
            sock = SimpleNamespace(makefile=lambda *a, **k: io.BytesIO(wire))
            response = http.client.HTTPResponse(sock)
            response.begin()
            return SimpleNamespace(request=lambda *a, **k: None,
                                   getresponse=lambda: response, close=lambda: response.close())
        status, body = R2UploadStore._persistent_network(Request('https://r2.invalid/object'),
                                                        1024, connection_factory=factory)
        self.assertEqual((status, body), (200, b'abcdef'))

    def test_large_error_page_is_retryable_but_oversized_success_is_rejected(self):
        for status, expected in ((503, R2RetryableError), (200, ValueError)):
            wire = ('HTTP/1.1 %d Response\r\nContent-Length: 2048\r\n\r\n' % status).encode() + b'x' * 2048
            sock = SimpleNamespace(makefile=lambda *a, **k: io.BytesIO(wire))
            response = http.client.HTTPResponse(sock)
            response.begin()
            factory = Mock(return_value=SimpleNamespace(request=lambda *a, **k: None,
                getresponse=lambda: response, close=lambda: response.close()))
            with self.subTest(status=status), self.assertRaises(expected):
                R2UploadStore._persistent_network(Request('https://r2.invalid/object'), 1024,
                                                  connection_factory=factory)

    def test_only_a_get_404_means_an_absent_immutable_object(self):
        for method in ('GET', 'PUT'):
            response = SimpleNamespace(status=404, close=lambda: None)
            factory = Mock(return_value=SimpleNamespace(request=lambda *a, **k: None,
                getresponse=lambda: response, close=lambda: None))
            req = Request('https://r2.invalid/object', method=method)
            if method == 'GET':
                self.assertEqual(R2UploadStore._persistent_network(req, 1024,
                                  connection_factory=factory), (404, b''))
            else:
                with self.assertRaises(OSError):
                    R2UploadStore._persistent_network(req, 1024, connection_factory=factory)

    def test_confirmed_failed_attempt_can_get_a_new_identity_without_changing_pins(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as name:
            directory = Path(name)
            first = delegate.conversion_generation(directory)
            second = delegate.conversion_generation(directory, advance=True)
            self.assertEqual(second, first + 1)
            job = {'pins': {'sha256': 'fixed'}, 'conversion_generation': first}
            self.assertNotEqual(identity(job), identity(dict(job, conversion_generation=second)))
            (directory / 'conversion-attempt.json').write_text('{"generation":true}')
            with self.assertRaises(ValueError):
                delegate.conversion_generation(directory)


if __name__ == '__main__':
    unittest.main()
