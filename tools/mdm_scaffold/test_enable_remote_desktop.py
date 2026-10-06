import plistlib
import unittest

from tools.mdm_scaffold.enable_remote_desktop import (
    DEFAULT_COMMAND_UUID,
    build_enable_remote_desktop_command,
    command_to_plist_bytes,
    parse_headers,
    validate_mdm_url,
)


class EnableRemoteDesktopTests(unittest.TestCase):
    def test_build_command_defaults(self) -> None:
        cmd = build_enable_remote_desktop_command()
        self.assertEqual(cmd["Command"]["RequestType"], "EnableRemoteDesktop")
        self.assertEqual(cmd["CommandUUID"], DEFAULT_COMMAND_UUID)

    def test_build_command_custom_uuid(self) -> None:
        cmd = build_enable_remote_desktop_command("abc-123")
        self.assertEqual(cmd["CommandUUID"], "abc-123")

    def test_payload_roundtrip(self) -> None:
        cmd = build_enable_remote_desktop_command("uuid-42")
        payload = command_to_plist_bytes(cmd)
        decoded = plistlib.loads(payload)
        self.assertEqual(decoded["Command"]["RequestType"], "EnableRemoteDesktop")
        self.assertEqual(decoded["CommandUUID"], "uuid-42")

    def test_parse_headers(self) -> None:
        headers = parse_headers(["Authorization: Bearer test", "X-Test: yes"])
        self.assertEqual(headers["Authorization"], "Bearer test")
        self.assertEqual(headers["X-Test"], "yes")

    def test_parse_headers_invalid(self) -> None:
        with self.assertRaises(ValueError):
            parse_headers(["bad-header"])

    def test_validate_url(self) -> None:
        self.assertEqual(
            validate_mdm_url("https://yourmdmhost.example.com/mdm"),
            "https://yourmdmhost.example.com/mdm",
        )

    def test_validate_url_invalid(self) -> None:
        with self.assertRaises(ValueError):
            validate_mdm_url("ftp://invalid.example.com/mdm")


if __name__ == "__main__":
    unittest.main()

