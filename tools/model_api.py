#!/usr/bin/env python3
"""Authenticated, local HTTP inference for the compact virtualOS models."""

from __future__ import annotations

import argparse
import base64
import binascii
from http.server import BaseHTTPRequestHandler, HTTPServer
import hmac
import ipaddress
import json
import os
from pathlib import Path
import socket
import tempfile

from critique_infer import score as score_critique
from design_infer import compare as compare_design
from hf_ui_router_train import features as route_features, predict as predict_route
import model_bundle

ROOT = Path(__file__).resolve().parent
MAX_BODY = 24_000_000
MAX_IMAGE = 10_000_000


def decode_image(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > 13_400_000:
        raise ValueError("bounded base64 image required")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64 image") from exc
    if not data or len(data) > MAX_IMAGE:
        raise ValueError("bounded image required")
    if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")):
        raise ValueError("PNG or JPEG image required")
    return data


class ModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "virtualOS-model-api/1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        # URLs, headers, and input text may contain private VM state.
        pass

    def respond(self, status: int, data: dict) -> None:
        payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.api_token  # type: ignore[attr-defined]
        return hmac.compare_digest(header, expected)

    def do_GET(self) -> None:
        if not self.authorized():
            self.respond(401, {"error": "unauthorized"})
        elif self.path == "/v1/health":
            self.respond(200, {"status": "ok", "models": ["route", "critique", "design"]})
        else:
            self.respond(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self.authorized():
            self.close_connection = True
            self.respond(401, {"error": "unauthorized"})
            return
        if self.path not in ("/v1/route", "/v1/critique", "/v1/design/compare"):
            self.close_connection = True
            self.respond(404, {"error": "not_found"})
            return
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal() or int(length) > MAX_BODY or int(length) == 0:
            self.close_connection = True
            self.respond(413, {"error": "invalid_content_length"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.close_connection = True
            self.respond(415, {"error": "json_required"})
            return
        try:
            request = json.loads(self.rfile.read(int(length)))
            if not isinstance(request, dict):
                raise ValueError("JSON object required")
            if self.path == "/v1/route":
                result = self.route(request)
            elif self.path == "/v1/critique":
                result = self.critique(request)
            else:
                result = self.design(request)
        except (ValueError, UnicodeError, KeyError, TypeError) as exc:
            self.respond(400, {"error": str(exc)})
            return
        except Exception:
            self.respond(500, {"error": "inference_failed"})
            return
        self.respond(200, result)

    def route(self, request: dict) -> dict:
        if set(request) != {"task"} or not isinstance(request["task"], str):
            raise ValueError("task string required")
        task = request["task"]
        if not task.strip() or len(task) > 1000:
            raise ValueError("task must contain 1 to 1000 characters")
        model = self.server.router_model  # type: ignore[attr-defined]
        prediction = predict_route(model, task)
        ratio = prediction["known_features"] / max(1, len(route_features(task)))
        prediction["known_feature_ratio"] = round(ratio, 4)
        prediction["status"] = "advice_only" if ratio >= 0.35 else "out_of_domain"
        return prediction

    def critique(self, request: dict) -> dict:
        if set(request) != {"text"} or not isinstance(request["text"], str):
            raise ValueError("text string required")
        value = request["text"]
        if not value.strip() or len(value.encode("utf-8")) > 20_000:
            raise ValueError("text must contain 1 to 20000 UTF-8 bytes")
        return score_critique(self.server.critique_model, value)  # type: ignore[attr-defined]

    def design(self, request: dict) -> dict:
        if set(request) != {"left_image_base64", "right_image_base64"}:
            raise ValueError("left_image_base64 and right_image_base64 required")
        left = decode_image(request["left_image_base64"])
        right = decode_image(request["right_image_base64"])
        with tempfile.TemporaryDirectory(prefix="vos-model-api-") as folder:
            left_path = Path(folder) / "left.png"
            right_path = Path(folder) / "right.png"
            left_path.write_bytes(left)
            right_path.write_bytes(right)
            return compare_design(left_path, right_path,
                                  self.server.design_models)  # type: ignore[attr-defined]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--models-bundle", type=Path,
                        help="compact binary bundle from model_bundle.py; default is plain JSON")
    args = parser.parse_args()
    try:
        address = ipaddress.ip_address(args.host)
    except ValueError as exc:
        parser.error(f"numeric IP address required: {exc}")
    if not address.is_loopback and not args.allow_remote:
        parser.error("non-loopback binding requires --allow-remote")
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    token = os.environ.get("VOS_MODEL_API_TOKEN", "")
    if len(token) < 32 or len(token) > 512 or any(c.isspace() for c in token):
        parser.error("set VOS_MODEL_API_TOKEN to a random 32..512 character token")
    bundle = args.models_bundle
    if bundle is not None:
        try:
            models = model_bundle.load_models(ROOT / "models", bundle)
        except (ValueError, OSError) as exc:
            parser.error(f"could not load models bundle: {exc}")
        router_model = models["ui_router/model.json"]
        critique_model = models["critique/model.json"]
        design_models = {kind: models[f"design/{kind}.json"] for kind in ("alignment", "overlap", "whitespace")}
    else:
        router_model = json.loads((ROOT / "models/ui_router/model.json").read_text())
        critique_model = json.loads((ROOT / "models/critique/model.json").read_text())
        design_models = None
    if router_model.get("schema") != "virtualos.hf-ui-router/v1":
        parser.error("unsupported router model")
    if critique_model.get("schema") != "virtualos.uicrit-text/v1":
        parser.error("unsupported critique model")
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    class Server(HTTPServer):
        address_family = family
        def get_request(self):
            connection, client_address = super().get_request()
            connection.settimeout(15)
            return connection, client_address
    with Server((args.host, args.port), ModelHandler) as server:
        server.api_token = token
        server.router_model = router_model
        server.critique_model = critique_model
        server.design_models = design_models if design_models is not None else ROOT / "models" / "design"
        print(f"virtualOS model API listening on {args.host}:{args.port}", flush=True)
        server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
