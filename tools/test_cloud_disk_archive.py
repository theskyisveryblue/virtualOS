import io
import tarfile
import tracemalloc
import threading
import unittest

from cloud_disk import CHUNK, digest
from cloud_disk_archive import stream_archive_upload


class Store:
    def __init__(self):
        self.objects = {}
        self.corrupt = False

    def put_if_absent(self, key, data):
        if key in self.objects:
            return False
        self.objects[key] = data
        return True

    def get(self, key, limit):
        return b"corrupt" if self.corrupt else self.objects[key]

    def get_if_present(self, key, limit):
        return self.get(key, limit) if key in self.objects else None


def fixture(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.disk = b"disk" * (CHUNK // 4) + bytes(CHUNK) + b"x" * 512
        self.archive = fixture([("./HardwareModel", b"platform"), ("./Disk.img", self.disk)])
        self.pins = dict(archive_bytes=len(self.archive), archive_sha256=digest(self.archive),
                         disk_bytes=len(self.disk), disk_sha256=digest(self.disk))

    def test_verified_manifest_and_idempotent_reuse(self):
        store = Store()
        result = stream_archive_upload(io.BytesIO(self.archive), store, **self.pins)
        self.assertEqual(result["chunk_count"], 3)
        self.assertFalse(result["local_disk_extracted"])
        repeat = stream_archive_upload(io.BytesIO(self.archive), store, **self.pins)
        self.assertEqual(repeat["uploaded_bytes"], 0)
        self.assertEqual(repeat["manifest_sha256"], result["manifest_sha256"])

    def test_bsdtar_sparse_disk_reconstructs_holes_and_next_member(self):
        # Stored size includes a padded map; logical size includes a 4 MiB hole.
        table = f"2\n0\n{CHUNK}\n{2 * CHUNK}\n512\n".encode().ljust(512, b"\0")
        payload = table + self.disk[:CHUNK] + self.disk[-512:]
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo('./Disk.img')
            info.size = len(payload)
            info.pax_headers = {'GNU.sparse.major': '1', 'GNU.sparse.minor': '0',
                                'GNU.sparse.name': './Disk.img',
                                'GNU.sparse.realsize': str(len(self.disk)),
                                'size': str(len(payload))}
            archive.addfile(info, io.BytesIO(payload))
            # Prove the parser lands on the following physical header correctly.
            link = tarfile.TarInfo('unsupported-link')
            link.type = tarfile.SYMTYPE
            link.linkname = 'Disk.img'
            archive.addfile(link)
        data = output.getvalue()
        store = Store()
        with self.assertRaisesRegex(ValueError, 'unsupported member'):
            stream_archive_upload(io.BytesIO(data), store,
                                  **dict(self.pins, archive_bytes=len(data), archive_sha256=digest(data)))
        self.assertNotIn('manifest.json', store.objects)
        # Repeat with a regular metadata member to verify full reconstruction.
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
            archive.addfile(info, io.BytesIO(payload))
            metadata = tarfile.TarInfo('HardwareModel')
            metadata.size = 8
            archive.addfile(metadata, io.BytesIO(b'platform'))
        data = output.getvalue()
        result = stream_archive_upload(io.BytesIO(data), Store(),
                                       **dict(self.pins, archive_bytes=len(data), archive_sha256=digest(data)))
        self.assertEqual(result['disk_sha256'], digest(self.disk))

    def test_bad_archive_or_disk_pin_never_publishes_manifest(self):
        for field in ("archive_sha256", "disk_sha256"):
            store = Store()
            with self.assertRaises(ValueError):
                stream_archive_upload(io.BytesIO(self.archive), store,
                                      **dict(self.pins, **{field: "0" * 64}))
            self.assertNotIn("manifest.json", store.objects)

    def test_parallel_upload_preserves_manifest_order_and_worker_bound(self):
        disk = self.disk[:CHUNK] + b'y' * CHUNK
        archive = fixture([('Disk.img', disk)])
        pins = dict(archive_bytes=len(archive), archive_sha256=digest(archive),
                    disk_bytes=len(disk), disk_sha256=digest(disk))
        gate = threading.Barrier(2)
        lock = threading.Lock()
        class ConcurrentStore(Store):
            active = peak = 0
            def put_if_absent(store, key, data):
                if key != 'manifest.json':
                    with lock:
                        store.active += 1
                        store.peak = max(store.peak, store.active)
                    gate.wait(timeout=3)
                    with lock:
                        store.active -= 1
                return super().put_if_absent(key, data)
        sequential = Store()
        expected = stream_archive_upload(io.BytesIO(archive), sequential, **pins)
        parallel = ConcurrentStore()
        actual = stream_archive_upload(io.BytesIO(archive), parallel, workers=2, **pins)
        self.assertEqual(actual['manifest_sha256'], expected['manifest_sha256'])
        self.assertEqual(parallel.objects['manifest.json'], sequential.objects['manifest.json'])
        self.assertEqual(parallel.peak, 2)

    def test_parallel_readback_failure_never_publishes_manifest(self):
        store = Store()
        store.corrupt = True
        with self.assertRaisesRegex(ValueError, 'readback'):
            stream_archive_upload(io.BytesIO(self.archive), store, workers=2, **self.pins)
        self.assertNotIn('manifest.json', store.objects)

    def test_missing_duplicate_and_short_disk_refused(self):
        for entries in ([('meta', b'only metadata')],
                        [('Disk.img', self.disk), ('Disk.img', self.disk)],
                        [('Disk.img', b'x' * 512)]):
            data = fixture(entries)
            store = Store()
            with self.assertRaises(ValueError):
                stream_archive_upload(io.BytesIO(data), store,
                                      **dict(self.pins, archive_bytes=len(data), archive_sha256=digest(data)))
            self.assertNotIn('manifest.json', store.objects)

    def test_corrupt_chunk_readback_never_publishes_manifest(self):
        store = Store()
        store.corrupt = True
        with self.assertRaises(ValueError):
            stream_archive_upload(io.BytesIO(self.archive), store, **self.pins)
        self.assertNotIn('manifest.json', store.objects)

    def test_restart_reads_existing_chunks_without_sending_another_put_body(self):
        from unittest.mock import patch
        store = Store()
        first = stream_archive_upload(io.BytesIO(self.archive), store, **self.pins)
        with patch.object(store, 'put_if_absent', wraps=store.put_if_absent) as puts:
            second = stream_archive_upload(io.BytesIO(self.archive), store, **self.pins)
        self.assertEqual([call.args[0] for call in puts.call_args_list], ['manifest.json'])
        self.assertEqual(first['manifest_sha256'], second['manifest_sha256'])
        self.assertEqual(second['uploaded_bytes'], 0)
        self.assertGreater(second['reused_bytes'], 0)

    def test_short_and_extra_compressed_bytes_refused(self):
        for data in (self.archive[:-10], self.archive + b'extra'):
            store = Store()
            with self.assertRaises((ValueError, tarfile.TarError, EOFError)):
                stream_archive_upload(io.BytesIO(data), store, **self.pins)
            self.assertNotIn('manifest.json', store.objects)

    def test_existing_manifest_is_preserved_on_conflict(self):
        store = Store()
        store.objects['manifest.json'] = b'existing pinned manifest'
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            stream_archive_upload(io.BytesIO(self.archive), store, **self.pins)
        self.assertEqual(store.objects['manifest.json'], b'existing pinned manifest')

    def test_streaming_allocations_do_not_require_a_disk_sized_buffer(self):
        tracemalloc.start()
        try:
            stream_archive_upload(io.BytesIO(self.archive), Store(), **self.pins)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # Bounds parser buffering + raw/compressed/readback chunks for this fixture.
        self.assertLess(peak, 48 * 1024**2)


if __name__ == '__main__':
    unittest.main()
