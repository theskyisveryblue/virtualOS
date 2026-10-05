#!/usr/bin/env python3
"""Integration checks for the local model API and bundled inference models."""

import base64
import binascii
import http.client
from http.server import HTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
import struct
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_api import ModelHandler, ROOT
import model_bundle


def load_bundle_models():
    source = {str(p.relative_to(ROOT / "models")):
              json.loads(p.read_text()) for p in sorted((ROOT / "models").rglob("*.json"))}
    return model_bundle.decode(model_bundle.encode(source))


class ModelApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), ModelHandler)
        cls.server.api_token = "test-token-0123456789-0123456789"
        cls.server.router_model = json.loads((ROOT / "models/ui_router/model.json").read_text())
        cls.server.critique_model = json.loads((ROOT / "models/critique/model.json").read_text())
        cls.server.design_models = ROOT / "models" / "design"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, body=None, token=None, method="POST", content_type="application/json"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        data = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_authorization_and_route(self):
        self.assertEqual(self.request("/v1/route", {"task": "Click the open tab"})[0], 401)
        status, result = self.request("/v1/route", {"task": "Click the open tab"}, self.server.api_token)
        self.assertEqual(status, 200)
        self.assertIn(result["choice"], ["cli", "structured_api", "dom_click", "form"])
        self.assertIn(result["status"], ["advice_only", "out_of_domain"])

    def test_critique_and_bounds(self):
        status, result = self.request("/v1/critique", {"text": "The dialog has tiny controls and poor contrast, which makes the action hard to find."}, self.server.api_token)
        self.assertEqual(status, 200)
        self.assertEqual(result["critique_model"], "advice_only")
        self.assertEqual(self.request("/v1/route", {"task": "x" * 1001}, self.server.api_token)[0], 400)
        self.assertEqual(self.request("/v1/critique", {"text": "x" * 20001}, self.server.api_token)[0], 400)

    def test_design_validation(self):
        status, result = self.request("/v1/design/compare", {"left_image_base64": base64.b64encode(b"not an image").decode(), "right_image_base64": ""}, self.server.api_token)
        self.assertEqual(status, 400)
        self.assertIn("image", result["error"])

    @staticmethod
    def png(rgb):
        def chunk(kind, value):
            return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", binascii.crc32(kind + value))
        header = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
        rows = (b"\x00" + bytes(rgb) * 8) * 8
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")

    def test_design_compare(self):
        left = base64.b64encode(self.png((255, 0, 0))).decode()
        right = base64.b64encode(self.png((0, 0, 255))).decode()
        status, result = self.request("/v1/design/compare", {"left_image_base64": left, "right_image_base64": right}, self.server.api_token)
        self.assertEqual(status, 200, result)
        self.assertEqual(set(result["design_principles"]), {"alignment", "overlap", "whitespace"})


class ModelBundleTest(unittest.TestCase):
    def test_roundtrip_shape_and_schema(self):
        models = load_bundle_models()
        original = {str(p.relative_to(ROOT / "models")):
                    json.loads(p.read_text()) for p in sorted((ROOT / "models").rglob("*.json"))}
        self.assertEqual(set(models), set(original))
        for name, model in models.items():
            self.assertEqual(model.get("schema"), original[name].get("schema"))

    def test_bundle_is_smaller_and_bounded_error(self):
        original = {str(p.relative_to(ROOT / "models")):
                    json.loads(p.read_text()) for p in sorted((ROOT / "models").rglob("*.json"))}
        blob = model_bundle.encode(original)
        text = sum(len(json.dumps(v, separators=(",", ":"))) for v in original.values())
        self.assertLess(len(blob), text)
        models = model_bundle.decode(blob)
        worst = 0.0

        def walk(a, b):
            nonlocal worst
            if isinstance(a, list):
                for left, right in zip(a, b):
                    walk(left, right)
            elif isinstance(a, dict):
                for key in a:
                    walk(a[key], b[key])
            elif isinstance(a, float):
                worst = max(worst, abs(a - b))

        walk(original["ui_router/model.json"], models["ui_router/model.json"])
        self.assertLess(worst, 0.01)

    def test_rejects_corruption(self):
        blob = bytearray(model_bundle.encode({"x": [1.0, 2.0, 3.0]}))
        blob[40] ^= 0xFF
        with self.assertRaises(ValueError):
            model_bundle.decode(bytes(blob))


class BundledApiTest(ModelApiTest):
    @classmethod
    def setUpClass(cls):
        models = load_bundle_models()
        cls.server = HTTPServer(("127.0.0.1", 0), ModelHandler)
        cls.server.api_token = "test-token-0123456789-0123456789"
        cls.server.router_model = models["ui_router/model.json"]
        cls.server.critique_model = models["critique/model.json"]
        cls.server.design_models = {kind: models[f"design/{kind}.json"]
                                    for kind in ("alignment", "overlap", "whitespace")}
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()


if __name__ == "__main__":
    unittest.main()
