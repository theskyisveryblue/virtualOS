#!/usr/bin/env python3
"""Read-only, metadata-only audit of the current guest screenshot."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from computer_use import GuestVM
from design_infer import features


def main() -> None:
    frame = GuestVM().frame(allow_blank=True)
    with tempfile.TemporaryDirectory(prefix="vos-frame-audit-") as directory:
        image = Path(directory) / "frame.jpg"
        image.write_bytes(frame.jpeg)
        image.chmod(0o600)
        values = features(image)
    gray_std = values[199]
    horizontal_edges = values[330]
    vertical_edges = values[398]
    result = {"frame_audit": "measured", "width": frame.width, "height": frame.height,
              "jpeg_bytes": len(frame.jpeg), "size_heuristic_blank": frame.likely_blank,
              "gray_std": round(gray_std, 5),
              "horizontal_edge_fraction": round(horizontal_edges, 5),
              "vertical_edge_fraction": round(vertical_edges, 5),
              "visually_blank": gray_std < 0.015 and
              horizontal_edges < 0.002 and vertical_edges < 0.002,
              "pixels_retained": False}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
