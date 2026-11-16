#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""CACTEE simulated certification service.

POST /certify  gzip tar of a package dir (config.json, code/, inputs/).
  runs test_docker.py, then crafts a fake asset certificate per .nar
  returns a gzip tar: result.json + *.nar + *_asset_certificate.json
GET  /health
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

BIND = os.environ.get("CACTEE_BIND", "0.0.0.0:8080")
CACTEE = Path(os.environ.get("CACTEE_ROOT", "/cactee"))
HARNESS = CACTEE / "examples" / "dev_test_docker"
OUTPUTS = HARNESS / "test_temp" / "tmp" / "outputs"
INPUTS = HARNESS / "test_temp" / "tmp" / "inputs"
MAX_BODY = int(os.environ.get("MAX_CERTIFY_BYTES", str(1 * 1024 * 1024 * 1024)))


def _find_config(root: Path) -> Path | None:
    direct = root / "config.json"
    if direct.is_file():
        return direct
    found = sorted(p for p in root.rglob("config.json") if p.is_file())
    return found[0] if found else None


def _require_build_ok(log: str) -> bool:
    built = (
        "building '" in log
        or "built locally" in log
        or "built with certified" in log
    )
    return built or "copying path" not in log


def _harness_tag(cfg: dict) -> str:
    tag = "test-" + str(cfg.get("name") or "")
    return tag.replace("/", "-").replace("_", "-").replace(" ", "-").lower()


def _reset_inputs() -> None:
    """Empty the harness inputs folder before each certification.

    test_docker.py keeps test_temp/tmp/inputs "as a cache" and never clears
    it, so every package would see the inputs of all previous ones. The real
    service uses a fresh folder per request; do the same here.
    """
    shutil.rmtree(INPUTS, ignore_errors=True)
    INPUTS.mkdir(parents=True, exist_ok=True)


def _list_nars(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.nar") if p.is_file())


def _pull_nars_from_container(tag: str, dest: Path) -> list[Path]:
    """Copy /tmp/outputs from the harness container (docker.sock)."""
    try:
        import docker
    except ImportError:
        return []
    dest.mkdir(parents=True, exist_ok=True)
    try:
        client = docker.from_env()
        container = client.containers.get(tag)
        bits, _ = container.get_archive("/tmp/outputs")
    except Exception as exc:  # noqa: BLE001
        print(f"cactee: docker pull outputs from {tag}: {exc}")
        return []
    buf = io.BytesIO()
    for chunk in bits:
        buf.write(chunk)
    buf.seek(0)
    try:
        with tarfile.open(fileobj=buf, mode="r") as tar:
            kwargs = {"filter": "data"} if sys.version_info >= (3, 12) else {}
            tar.extractall(dest, **kwargs)
    except tarfile.TarError as exc:
        print(f"cactee: outputs archive from {tag}: {exc}")
        return []
    return _list_nars(dest)


# Same four fields as Nokia AssetCertificate.get_asset_certificate().
# The signature is RSA-PSS SHA-256 over json.dumps(duet_service_result, sort_keys=True),
# which is what DuetAssetCertificationServiceClient.get_result() checks.
_MRENCLAVE = bytes.fromhex(os.environ.get("CACTEE_MRENCLAVE", "11" * 32))
_SERVICE_CODE_HASH = os.environ.get("CACTEE_SERVICE_CODE_HASH", "ab" * 32)
_SERVICE_DEP_HASH = os.environ.get("CACTEE_SERVICE_DEPENDENCIES_HASH", "cd" * 32)
_SERVICE_TOOLS = json.loads(os.environ.get(
    "CACTEE_SERVICE_TOOLS_HASHES",
    '{"build_tools.py":"' + "ef" * 32 + '"}',
))
_ATTEST_URL = os.environ.get(
    "CACTEE_ATTESTATION_URL", "https://sharedneu.neu.attest.azure.net")
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PEM = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


def _controller_quote_b64() -> str:
    """432-byte quote. Verifier reads MRENCLAVE at [112:144] and SHA-256(PEM) at [368:400]."""
    quote = bytearray(432)
    quote[112:144] = _MRENCLAVE
    quote[368:400] = hashlib.sha256(_PEM.encode()).digest()
    return base64.b64encode(bytes(quote)).decode()


def _sign_service_result(service_result: dict) -> str:
    payload = json.dumps(service_result, sort_keys=True).encode()
    signature = _KEY.sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def _fake_certificate(nar: Path, cfg: dict) -> dict:
    """Nokia asset-certificate shape, plus the four fields the cache import API requires."""
    nar_bytes = nar.read_bytes()
    digest = hashlib.sha256(nar_bytes).digest()
    quote = _controller_quote_b64()
    inputs = {}
    for item in cfg.get("inputs") or []:
        if isinstance(item, dict) and item.get("name"):
            inputs[item["name"]] = item.get("hash") or "00" * 32
    service_result = {
        "service_info": {
            "service_code_hash": _SERVICE_CODE_HASH,
            "service_dependencies_hash": _SERVICE_DEP_HASH,
            "service_location": {
                "simulated": {
                    "service_owner_config_result": "simulated",
                }
            },
            "service_tools_hashes": _SERVICE_TOOLS,
        },
        "service_result": {
            "certificates": {},
            "hashes": {
                "code": {},
                "code_build_args": {},
                "inputs": inputs,
                "outputs": {nar.name: digest.hex()},
            },
            "power_usage": {"codecarbon": {"99_total_power_usage_KWh": 0}},
            "timestamp": int(time.time()),
        },
    }
    return {
        "duet_admin_quote": {"quote": quote, "public_key": _PEM},
        "duet_admin_attestation_service_url": _ATTEST_URL,
        "duet_service_result": service_result,
        "duet_admin_signature": _sign_service_result(service_result),
        "type": "simulated-v1",
        "backend": "nixtee-cactee",
        "quote": quote,
        "outputHash": "sha256-" + base64.b64encode(digest).decode(),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"cactee: {self.address_string()} {fmt % args}")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _pack_result(self, result: dict, nars: list[Path], extras: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        payload = json.dumps(result).encode()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("result.json")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
            for nar in nars:
                tar.add(nar, arcname=nar.name)
            for name, raw in extras.items():
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))
        return buf.getvalue()

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "harness": str(HARNESS),
                    "cactee_root": str(CACTEE),
                    "simulated_certs": True,
                },
            )
            return
        self._send_json(404, {"error": "GET /health or POST /certify"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/certify":
            self._send_json(404, {"error": "POST /certify"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Content-Length required"})
            return
        if length <= 0:
            self._send_json(400, {"error": "empty body; send a gzip tar of the package dir"})
            return
        if length > MAX_BODY:
            self._send_json(413, {"error": f"body {length} exceeds MAX_CERTIFY_BYTES={MAX_BODY}"})
            return
        raw = self.rfile.read(length)
        require_build = (self.headers.get("X-Require-Build") or "0").strip() == "1"
        work = Path(tempfile.mkdtemp(prefix="certify-"))
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
                tar.extractall(work, filter="data")
        except tarfile.TarError as exc:
            self._send_json(400, {"error": f"not a tar: {exc}"})
            return
        cfg_path = _find_config(work)
        if cfg_path is None:
            self._send_json(400, {"error": "config.json missing from uploaded package"})
            return
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        print(f"cactee: certify {cfg.get('name')} via test_docker.py", flush=True)
        _reset_inputs()
        proc = subprocess.Popen(
            [sys.executable, "test_docker.py", str(cfg_path)],
            cwd=HARNESS,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log_lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            print(f"cactee: {line}", end="", flush=True)
            log_lines.append(line)
        rc = proc.wait()
        log = "".join(log_lines)
        nars = _list_nars(OUTPUTS)
        if not nars:
            pulled = Path(tempfile.mkdtemp(prefix="cactee-nars-"))
            nars = _pull_nars_from_container(_harness_tag(cfg), pulled)
            if nars:
                log += f"\npulled {len(nars)} .nar from container {_harness_tag(cfg)}:/tmp/outputs\n"
        rc = proc.returncode
        if rc == 0 and require_build and not _require_build_ok(log):
            rc = 1
            log += (
                "\nno 'building' line in the log: the target was substituted, "
                "not built. Certifying this would attest a copy.\n"
            )
        if rc == 0 and not nars:
            rc = 1
            log += f"\nno .nar produced in {OUTPUTS}\n"
        extras: dict[str, bytes] = {}
        cert_names: list[str] = []
        if rc == 0:
            last = None
            for nar in nars:
                cert = _fake_certificate(nar, cfg)
                last = json.dumps(cert, indent=2).encode() + b"\n"
                extras[f"{nar.name}_asset_certificate.json"] = last
                cert_names.append(f"{nar.name}_asset_certificate.json")
            if last is not None:
                extras["asset_certificate.json"] = last
        result = {
            "rc": rc,
            "nars": [p.name for p in nars],
            "certificates": cert_names,
            "log_tail": log[-8000:],
        }
        body = self._pack_result(result, nars if rc == 0 else [], extras)
        self._send(200 if rc == 0 else 500, body, "application/gzip")


def main() -> None:
    host, port_s = BIND.rsplit(":", 1)
    if not HARNESS.is_dir():
        raise SystemExit(f"CACTEE harness missing: {HARNESS}")
    print(f"cactee listening on {BIND} harness={HARNESS} simulated_certs=true")
    print(f"cactee certificate style: duet_admin_quote, duet_service_result, duet_admin_signature")
    print(f"cactee simulated MRENCLAVE {_MRENCLAVE.hex()}")
    print(f"cactee service_code_hash {_SERVICE_CODE_HASH}")
    print(f"cactee service_dependencies_hash {_SERVICE_DEP_HASH}")
    ThreadingHTTPServer((host, int(port_s)), Handler).serve_forever()


if __name__ == "__main__":
    main()
