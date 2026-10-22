import json
from contextlib import closing
import os
from pathlib import Path
import socket
import random
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from cloud_disk import BLOCK, NBD_MAGIC, NBD_OPTION_MAGIC, NBD_REQUEST_MAGIC, NBD_SIMPLE_REPLY_MAGIC, receive_exact
from compressed_delta import build, file_hash


class CompressedNBDTests(unittest.TestCase):
    compressed_writes = False

    def test_wire_reads_fua_reconnect_and_server_restart_keep_seed_separate(self):
        with tempfile.TemporaryDirectory(prefix='vos-compressed-nbd-',dir='/private/tmp') as directory:
            root=Path(directory);bundle=root/'base.bundle';bundle.mkdir()
            base=bundle/'Disk.img';base.write_bytes(b'a'*(8*BLOCK))
            machine=root/'machine.raw';machine.write_bytes(b'b'*(8*BLOCK))
            delta=root/'delta.sqlite';receipt=build(base,machine,delta,reserve=0)
            overlay=root/'.virtualos-nbd/vm-a/overlay.sqlite'
            for _ in range(40):
                port=random.randrange(20000,60999)
                with socket.socket() as probe:
                    try:probe.bind(('127.0.0.1',port))
                    except OSError:continue
                break
            else:self.fail('no test port available')
            environment=dict(os.environ,VOS_NBD_MIN_HOST_FREE='0',VOS_NBD_PERSISTENT='1',
                             VOS_NBD_COMPRESSED_DELTA_SHA256=receipt['delta_sha256'],
                             VOS_NBD_COMPRESSED_WRITES='1' if self.compressed_writes else '0')
            command=[sys.executable,str(Path(__file__).with_name('cloud_disk.py')),'base-delta-serve',
                     str(root),'base',str(overlay),str(port),str(delta)]
            child=None
            def stop():
                if child is not None and child.poll() is None:
                    if self.compressed_writes:
                        child.kill()
                    else:
                        child.terminate()
                    child.wait(timeout=5)
            def connect():
                client=socket.create_connection(('127.0.0.1',port),timeout=5)
                self.assertEqual(struct.unpack('>QQH',receive_exact(client,18))[0],NBD_MAGIC)
                export=b'virtualos-test'
                client.sendall(struct.pack('>I',1)+struct.pack('>QII',NBD_OPTION_MAGIC,1,len(export))+export)
                self.assertEqual(struct.unpack('>QH',receive_exact(client,10))[0],8*BLOCK)
                receive_exact(client,124)
                return client
            def read(client,index,cookie):
                client.sendall(struct.pack('>IHHQQI',NBD_REQUEST_MAGIC,0,0,cookie,index*BLOCK,BLOCK))
                self.assertEqual(struct.unpack('>IIQ',receive_exact(client,16)),(NBD_SIMPLE_REPLY_MAGIC,0,cookie))
                return receive_exact(client,BLOCK)
            try:
                for generation in range(2):
                    log=root/('server%d.log'%generation)
                    with log.open('wb') as output:
                        child=subprocess.Popen(command,env=environment,stdin=subprocess.DEVNULL,stdout=output,stderr=output)
                    for _ in range(100):
                        if '"base_delta_listener": "ready"' in log.read_text():break
                        if child.poll() is not None:self.fail(log.read_text())
                        time.sleep(.05)
                    else:self.fail('scratch compressed NBD readiness timeout')
                    with connect() as client:
                        self.assertEqual(read(client,1,1),b'b'*BLOCK)
                        if generation==0:
                            client.sendall(struct.pack('>IHHQQI',NBD_REQUEST_MAGIC,1,1,2,2*BLOCK,BLOCK)+b'x'*BLOCK)
                            self.assertEqual(struct.unpack('>IIQ',receive_exact(client,16)),(NBD_SIMPLE_REPLY_MAGIC,0,2))
                        self.assertEqual(read(client,2,3),b'x'*BLOCK)
                        if self.compressed_writes and generation==0:
                            # Kill with the acknowledged FUA session still open;
                            # no graceful disconnect/close may supply durability.
                            stop()
                        else:
                            client.sendall(struct.pack('>IHHQQI',NBD_REQUEST_MAGIC,0,2,99,0,0))
                    if not (self.compressed_writes and generation==0):
                        with connect() as client:
                            self.assertEqual(read(client,2,4),b'x'*BLOCK)
                            client.sendall(struct.pack('>IHHQQI',NBD_REQUEST_MAGIC,0,2,99,0,0))
                    stop()
                    with closing(sqlite3.connect(overlay)) as database:
                        if self.compressed_writes:
                            from compressed_write_overlay import decode
                            database.create_function('vos_overlay_decode_v1',1,decode)
                            stored=database.execute('SELECT SUM(LENGTH(data)) FROM encoded_blocks').fetchone()[0]
                            self.assertLess(stored,BLOCK//10)
                        self.assertEqual(database.execute('SELECT COUNT(*) FROM blocks').fetchone()[0],1)
                    ready=json.loads(next(line for line in log.read_text().splitlines() if '"base_delta_listener"' in line))
                    self.assertEqual(ready['delta_seeded'],0)
                    self.assertEqual(ready['compressed_delta_sha256'],receipt['delta_sha256'])
                    self.assertEqual(ready['compressed_writes'],self.compressed_writes)
                self.assertEqual(file_hash(delta),receipt['delta_sha256'])
                self.assertEqual(base.read_bytes(),b'a'*(8*BLOCK))
            finally:stop()


class CompressedWriteNBDTests(CompressedNBDTests):
    compressed_writes = True


if __name__=='__main__':unittest.main()
