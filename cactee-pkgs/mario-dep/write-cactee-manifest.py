#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""Write cactee-manifest.json in this folder.

On the publisher (this folder is mounted at /mario-dep):

  docker exec -w /mario-dep nixtee-publisher python3 write-cactee-manifest.py

Reads duet_expected_hashes.json from --cactee, then from /home/aetisam/cactee,
then from this folder.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEYS = ("EXPECTED_DUET_ADMIN_MRENCLAVE", "EXPECTED_DUET_SERVICE_INFO")


def find_source() -> Path:
    if len(sys.argv) == 3 and sys.argv[1] == "--cactee":
        candidate = Path(sys.argv[2]) / "duet_expected_hashes.json"
        if candidate.is_file():
            return candidate
        sys.exit(f"not found: {candidate}")
    for candidate in (
        Path("/home/aetisam/cactee/duet_expected_hashes.json"),
        HERE / "duet_expected_hashes.json",
    ):
        if candidate.is_file():
            return candidate
    sys.exit(
        "duet_expected_hashes.json not found. "
        "Pass --cactee /path/to/cactee or copy that file next to this script."
    )


def main() -> None:
    data = json.loads(find_source().read_text(encoding="utf-8"))
    missing = [key for key in KEYS if key not in data]
    if missing:
        sys.exit("missing keys: " + ", ".join(missing))
    manifest = {key: data[key] for key in KEYS}
    dest = HERE / "cactee-manifest.json"
    dest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(dest)


if __name__ == "__main__":
    main()
