#!/usr/bin/env python3
"""virtualOS fleet manager (experimental).

A thin, dependency-free CLI/orchestrator over the headless FleetSupervisor.
The supervisor owns VM lifecycle; this tool talks to its local HTTP surface and,
when a multiplexer is running, exposes every VM through one endpoint.

    tools/fleet/fleet.py status
    tools/fleet/fleet.py capability
    tools/fleet/fleet.py up vm-a,vm-b
    tools/fleet/fleet.py start vm-a
    tools/fleet/fleet.py stop vm-a
    tools/fleet/fleet.py serve --http-port 9300   # multiplexer

The multiplexer is the "stream them all out of one pipe" piece: a single
process that fans out to all VMs and can be fronted by the Cloudflare relay.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_SUPERVISOR = os.environ.get("VOS_FLEET_SUPERVISOR", "http://127.0.0.1:9200")


def _request(method: str, url: str, payload: dict | None = None, timeout: float = 10.0):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp else ""
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"error": raw}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        return 0, {"ok": False, "error": f"supervisor unreachable: {exc.reason}"}


def supervisor_get(base: str, path: str):
    return _request("GET", f"{base}{path}")


def supervisor_post(base: str, path: str):
    return _request("POST", f"{base}{path}")


def loopback_host(host):
    if host not in ('127.0.0.1', 'localhost'):
        raise ValueError('multiplexer requires a loopback host')
    return '127.0.0.1'


def backend_address(base):
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('multiplexer requires a plain loopback supervisor URL')
    port = parsed.port or 80
    return loopback_host(parsed.hostname), port


def shutdown_socket(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class HeaderBudget:
    def __init__(self, stream):
        self.stream, self.remaining = stream, 8192

    def readline(self, size=-1):
        line = self.stream.readline(min(size if size >= 0 else 8193, self.remaining + 1))
        self.remaining -= len(line)
        if self.remaining < 0:
            raise http.client.LineTooLong('request headers')
        return line

    def __getattr__(self, name):
        return getattr(self.stream, name)


class MultiplexerServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, handler, supervisor):
        address = (loopback_host(address[0]), address[1])
        backend_address(supervisor)
        self.supervisor = supervisor
        self.clients = threading.BoundedSemaphore(8)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.clients.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.clients.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.clients.release()


class MultiplexerHandler(BaseHTTPRequestHandler):
    supervisor = DEFAULT_SUPERVISOR

    def log_message(self, *_args):  # quiet by default
        pass

    def handle(self):
        self.deadline = time.monotonic() + 5
        self.connection.settimeout(5)
        timer = threading.Timer(5, shutdown_socket, args=(self.connection,))
        timer.daemon = True
        timer.start()
        self.rfile = HeaderBudget(self.rfile)
        try:
            super().handle()
        except (OSError, http.client.HTTPException):
            pass
        finally:
            timer.cancel()
            timer.join()

    def parse_request(self):
        if not super().parse_request():
            return False
        self.close_connection = True
        hosts = self.headers.get_all('Host', [])
        expected = {f'127.0.0.1:{self.server.server_port}',
                    f'localhost:{self.server.server_port}'}
        lengths = self.headers.get_all('Content-Length', [])
        fetch = self.headers.get_all('Sec-Fetch-Site', [])
        if (len(hosts) != 1 or hosts[0] not in expected
                or self.headers.get_all('Origin', [])
                or fetch and fetch != ['none']):
            self._json(403, {'ok': False, 'error': 'native loopback clients only'})
            return False
        # These proxy routes consume no request body. Refuse ambiguous framing
        # before any backend request, including duplicate zero lengths.
        if (self.headers.get_all('Transfer-Encoding', []) or len(lengths) > 1
                or lengths and lengths != ['0']
                or self.headers.get_all('Expect', [])):
            self._json(400, {'ok': False, 'error': 'request framing refused'})
            return False
        return True

    def _backend(self, method, path, maximum):
        host, port = backend_address(self.server.supervisor)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('request deadline')
        connection = http.client.HTTPConnection(host, port, timeout=min(remaining, 2))
        timer = None
        try:
            connection.connect()
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('request deadline')
            connection.sock.settimeout(remaining)
            timer = threading.Timer(remaining, shutdown_socket, args=(connection.sock,))
            timer.daemon = True
            timer.start()
            connection.request(method, path, headers={'Accept': '*/*', 'Connection': 'close'})
            response = connection.getresponse()
            body = response.read(maximum + 1)
            if len(body) > maximum:
                raise ValueError('backend response exceeds bound')
            return response.status, response.getheader('Content-Type', 'application/json'), body
        finally:
            if timer:
                timer.cancel()
                timer.join()
            connection.close()

    def _json(self, code: int, obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _proxy_frame(self, vm: str) -> None:
        try:
            code, ctype, data = self._backend('GET', '/fleet/frame/' + vm, 8 * 1024 * 1024)
            self.send_response(code)
            self.send_header("Content-Type", 'image/jpeg' if ctype.split(';')[0] == 'image/jpeg' else 'application/octet-stream')
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (OSError, ValueError, http.client.HTTPException):
            self._json(502, {"ok": False, "error": "supervisor unavailable"})

    def _proxy_json(self, method, path):
        try:
            code, _, data = self._backend(method, path, 1024 * 1024)
            body = json.loads(data) if data else {}
            if not isinstance(body, dict):
                raise ValueError('invalid backend response')
            return self._json(code, body)
        except (OSError, ValueError, http.client.HTTPException):
            return self._json(502, {'ok': False, 'error': 'supervisor unavailable'})

    def do_OPTIONS(self):
        self._json(405, {'ok': False, 'error': 'method refused'})

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "service": "virtualos-fleet-mux"})
        if re.fullmatch(r'/fleet/frame/[A-Za-z0-9_-]{1,128}(?:\?t=[0-9]{1,20})?', self.path):
            vm = self.path.split("/fleet/frame/", 1)[1].split("?", 1)[0].strip("/")
            if not vm:
                return self._json(400, {"ok": False, "error": "missing vm name"})
            return self._proxy_frame(vm)
        if self.path in ("/fleet/status", "/fleet/capability"):
            return self._proxy_json('GET', self.path)
        return self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path == "/fleet/start" or re.fullmatch(r'/fleet/(?:start|stop)/[A-Za-z0-9_-]{1,128}', self.path):
            return self._proxy_json('POST', self.path)
        return self._json(404, {"ok": False, "error": "not found"})


def cmd_status(args) -> int:
    code, body = supervisor_get(args.supervisor, "/fleet/status")
    if code == 0:
        print(body.get("error", "unreachable"), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(body, indent=2))
        return 0
    cap = body.get("capability", {})
    print(f"root={body.get('root')} count={body.get('count')} "
          f"nested={cap.get('nestedVirtualizationSupported')}")
    for inst in body.get("instances", []):
        flag = "ready" if inst.get("ready") else inst.get("state", "?")
        err = f" err={inst['error']}" if inst.get("error") else ""
        print(f"  {inst.get('name'):<12} {flag:<10} port={inst.get('controlPort')}{err}")
    return 0


def cmd_capability(args) -> int:
    code, body = supervisor_get(args.supervisor, "/fleet/capability")
    print(json.dumps(body, indent=2))
    return 0 if code else 1


def cmd_up(args) -> int:
    names = args.names
    code, body = supervisor_post(args.supervisor, "/fleet/start")
    if code == 0:
        print(body.get("error"), file=sys.stderr)
        return 1
    print(json.dumps(body, indent=2) if args.json else f"requested start of {names or 'all'}")
    return 0


def cmd_start(args) -> int:
    code, body = supervisor_post(args.supervisor, f"/fleet/start/{args.name}")
    print(json.dumps(body, indent=2))
    return 0 if body.get("ok") else 1


def cmd_stop(args) -> int:
    code, body = supervisor_post(args.supervisor, f"/fleet/stop/{args.name}")
    print(json.dumps(body, indent=2))
    return 0 if body.get("ok") else 1


def cmd_serve(args) -> int:
    server = MultiplexerServer((args.host, args.port), MultiplexerHandler, args.supervisor)
    print(f"virtualos fleet mux on http://{args.host}:{args.port} -> {args.supervisor}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet")
    parser.add_argument("--supervisor", default=DEFAULT_SUPERVISOR,
                        help="FleetSupervisor base URL")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="show fleet status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("capability", help="probe nested virtualization support")
    p.set_defaults(func=cmd_capability)

    p = sub.add_parser("up", help="start all (or named) VMs")
    p.add_argument("names", nargs="?", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("start", help="start one VM")
    p.add_argument("name")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="stop one VM")
    p.add_argument("name")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("serve", help="run the fleet multiplexer")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9300)
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return args.func(args)
    if getattr(args, "json", False) and not hasattr(args, "func"):
        pass
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
