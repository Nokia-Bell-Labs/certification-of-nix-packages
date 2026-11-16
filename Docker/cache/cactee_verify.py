#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""Verify a CACTEE asset certificate on the cache side.

Same checks as the client-side verifier in the Nix fork
(nix-fork/nix-src/src/certification-verification/certificate-verifier.cc),
ported to Python so the cache import API can refuse uncertified packages.

A certificate has the shape produced by CACTEE (AssetCertificate):

  duet_admin_quote                 {quote: base64 SGX quote, public_key: PEM, jwt?: Azure token}
  duet_admin_attestation_service_url
  duet_service_result              {service_info: {...}, service_result: {hashes: {outputs: {...}}}}
  duet_admin_signature             base64 RSA-PSS-SHA256 over json.dumps(duet_service_result, sort_keys=True)

Steps (names match the C++ verifier):

  01_duet_admin_quote      the quote binds the controller's public key and MRENCLAVE;
                           the Azure attestation token (JWT) vouches for the quote
  02_controller_signature  the controller signed the service result with that key
  03_duet_service_info     the service that ran is the expected one (manifest)
  05_output_hashes         the NAR we hold is the one the certificate names

Steps 01-03 need only the certificate. Step 05 needs the NAR bytes: the
import API runs it after receiving the NAR and before registering it.

Crypto is done with the `openssl` command, so the cache needs no Python
package beyond the standard library.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# Offsets in an SGX DCAP quote (48-byte header + 384-byte report body).
QUOTE_MIN_LEN = 400
MRENCLAVE_AT = 112   # report body + 64
REPORT_DATA_AT = 368  # report body + 320, first 32 bytes = sha256(PEM)

MANIFEST_KEYS = ("EXPECTED_DUET_ADMIN_MRENCLAVE", "EXPECTED_DUET_SERVICE_INFO")


class CertificateError(Exception):
    """The certificate is malformed or fails a check."""


@dataclass
class Step:
    name: str
    ok: bool = False
    detail: list[str] = field(default_factory=list)


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    def failures(self) -> str:
        return "; ".join(
            f"{s.name}: {', '.join(s.detail) or 'failed'}" for s in self.steps if not s.ok
        )

    def summary(self) -> str:
        return " ".join(f"{s.name}={'ok' if s.ok else 'FAIL'}" for s in self.steps)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def load_manifest(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [k for k in MANIFEST_KEYS if k not in data]
    if missing:
        raise CertificateError(f"manifest {path} lacks {', '.join(missing)}")
    return data


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(s: str) -> bytes:
    return base64.b64decode(s, validate=False)


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical_service_result(service_result: dict) -> bytes:
    """Bytes the controller signed: Python's json.dumps with sorted keys."""
    return json.dumps(service_result, sort_keys=True).encode()


def _openssl_verify(pubkey_pem: bytes, message: bytes, signature: bytes, pss: bool) -> bool:
    with tempfile.TemporaryDirectory(prefix="cactee-verify-") as d:
        key, msg, sig = Path(d, "key.pem"), Path(d, "msg"), Path(d, "sig")
        key.write_bytes(pubkey_pem)
        msg.write_bytes(message)
        sig.write_bytes(signature)
        cmd = ["openssl", "dgst", "-sha256"]
        if pss:
            # Same parameters as the signer: PSS, MGF1-SHA256, maximum salt.
            cmd += ["-sigopt", "rsa_padding_mode:pss",
                    "-sigopt", "rsa_mgf1_md:sha256",
                    "-sigopt", "rsa_pss_saltlen:max"]
        cmd += ["-verify", str(key), "-signature", str(sig), str(msg)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.returncode == 0 and "Verified OK" in proc.stdout


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _der(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(body)) + body


def _der_int(raw: bytes) -> bytes:
    raw = raw.lstrip(b"\x00") or b"\x00"
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der(0x02, raw)


def jwk_to_pem(jwk: dict) -> bytes:
    """RSA JWK (n, e) -> SubjectPublicKeyInfo PEM, for `openssl dgst -verify`."""
    if jwk.get("kty", "RSA") != "RSA" or "n" not in jwk or "e" not in jwk:
        raise CertificateError("attestation signing key is not an RSA JWK")
    rsa_key = _der(0x30, _der_int(_b64url(jwk["n"])) + _der_int(_b64url(jwk["e"])))
    rsa_oid = bytes.fromhex("2a864886f70d010101")  # 1.2.840.113549.1.1.1
    algo = _der(0x30, _der(0x06, rsa_oid) + b"\x05\x00")
    spki = _der(0x30, algo + _der(0x03, b"\x00" + rsa_key))
    b64 = base64.b64encode(spki).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return ("-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines)
            + "\n-----END PUBLIC KEY-----\n").encode()


def azure_endpoint(url: str, allow_any_host: bool = False) -> str:
    """The attestation URL comes from the (untrusted) certificate: accept only
    an Azure Attestation host over https, without path, user or query."""
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.query or p.path not in ("", "/"):
        raise CertificateError(f"attestation service url {url!r} is not https://<host>")
    host = p.hostname
    if not allow_any_host and not (host == "attest.azure.net" or host.endswith(".attest.azure.net")):
        raise CertificateError(f"attestation service url {url!r} is not an Azure Attestation host")
    return f"https://{p.netloc}"


_JWKS_CACHE: dict[str, dict] = {}


def fetch_jwks(endpoint: str) -> dict:
    if endpoint not in _JWKS_CACHE:
        req = urllib.request.Request(endpoint + "/certs", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            _JWKS_CACHE[endpoint] = json.loads(resp.read(1 << 20))
    return _JWKS_CACHE[endpoint]


def verify_jwt(token: str, endpoint: str, jwks: dict, quote: bytes, pem: bytes,
               expected_mrenclave: str) -> list[str]:
    """Check the Azure attestation token embedded in the certificate.
    Returns a list of problems (empty = valid)."""
    parts = token.split(".")
    if len(parts) != 3:
        return ["attestation token is not a JWT"]
    try:
        header = json.loads(_b64url(parts[0]))
        payload = json.loads(_b64url(parts[1]))
        signature = _b64url(parts[2])
    except (ValueError, json.JSONDecodeError):
        return ["attestation token could not be decoded"]
    if header.get("alg") != "RS256":
        return ["attestation token algorithm is not RS256"]
    keys = jwks.get("keys") or []
    kid = header.get("kid")
    if kid is not None:
        jwk = next((k for k in keys if k.get("kid") == kid), None)
        if jwk is None:
            return ["attestation token key id is not in the service keys"]
    elif len(keys) == 1:
        jwk = keys[0]
    else:
        return ["attestation token has no key id"]
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    if not _openssl_verify(jwk_to_pem(jwk), signing_input, signature, pss=False):
        return ["attestation token signature does not match"]

    problems = []
    if str(payload.get("iss", "")).rstrip("/") != endpoint.rstrip("/"):
        problems.append("token issuer is not the attestation service")
    mr = payload.get("sgx-mrenclave")
    if mr != expected_mrenclave:
        problems.append("token mrenclave does not match the manifest")
    if mr != quote[MRENCLAVE_AT:MRENCLAVE_AT + 32].hex():
        problems.append("token mrenclave does not match the quote")
    report_key = str(payload.get("x-ms-sgx-report-data", ""))[:64]
    if report_key != sha256_hex(pem):
        problems.append("token report data does not match the public key")
    if report_key != quote[REPORT_DATA_AT:REPORT_DATA_AT + 32].hex():
        problems.append("token report data does not match the quote")
    quotehash = (payload.get("x-ms-sgx-collateral") or {}).get("quotehash")
    if quotehash != sha256_hex(quote):
        problems.append("token quote hash does not match the quote")
    # Not checked: exp/nbf. The token is minted at build time and the
    # certificate is verified later (publish, install), so it is usually
    # expired by then. Open question for the certificate format.
    return problems


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def verify_certificate(cert: dict, manifest: dict, *, simulate_azure: bool = False,
                       jwks: dict | None = None) -> Report:
    """Steps 01-03: everything that does not need the NAR bytes."""
    report = Report()
    expected_mr = manifest["EXPECTED_DUET_ADMIN_MRENCLAVE"]
    expected_info = manifest["EXPECTED_DUET_SERVICE_INFO"]

    # 01 -- quote
    s1 = Step("01_duet_admin_quote")
    report.steps.append(s1)
    q = cert.get("duet_admin_quote") or {}
    pem_s = q.get("public_key")
    quote_s = q.get("quote")
    url = cert.get("duet_admin_attestation_service_url")
    if not isinstance(pem_s, str) or not isinstance(quote_s, str):
        s1.detail.append("quote or public key is missing")
    elif not isinstance(url, str) or not url:
        s1.detail.append("attestation service url is missing")
    else:
        pem = pem_s.encode()
        quote = _b64(quote_s)
        if len(quote) < QUOTE_MIN_LEN:
            s1.detail.append(f"quote is {len(quote)} bytes, expected >= {QUOTE_MIN_LEN}")
        else:
            key_ok = quote[REPORT_DATA_AT:REPORT_DATA_AT + 32].hex() == sha256_hex(pem)
            mr_ok = quote[MRENCLAVE_AT:MRENCLAVE_AT + 32].hex() == expected_mr
            if not key_ok:
                s1.detail.append("public key hash does not match quote report_data")
            if not mr_ok:
                s1.detail.append("mrenclave does not match the manifest")
            jwt_ok = False
            try:
                endpoint = azure_endpoint(url, allow_any_host=simulate_azure)
                if simulate_azure:
                    jwt_ok = True
                    s1.detail.append("azure simulated: attestation token not checked")
                elif not isinstance(q.get("jwt"), str):
                    s1.detail.append("attestation token (duet_admin_quote.jwt) is missing")
                else:
                    problems = verify_jwt(q["jwt"], endpoint, jwks or fetch_jwks(endpoint),
                                          quote, pem, expected_mr)
                    jwt_ok = not problems
                    s1.detail.extend(problems)
            except CertificateError as exc:
                s1.detail.append(str(exc))
            except OSError as exc:
                s1.detail.append(f"cannot fetch attestation service keys: {exc}")
            s1.ok = key_ok and mr_ok and jwt_ok

    # 02 -- controller signature over the service result
    s2 = Step("02_controller_signature")
    report.steps.append(s2)
    result = cert.get("duet_service_result")
    sig_s = cert.get("duet_admin_signature")
    if not isinstance(result, dict) or not isinstance(sig_s, str) or not isinstance(pem_s, str):
        s2.detail.append("signature, service result or public key is missing")
    else:
        s2.ok = _openssl_verify(pem_s.encode(), canonical_service_result(result), _b64(sig_s), pss=True)
        if not s2.ok:
            s2.detail.append("signature does not match")

    # 03 -- the service that ran is the expected one
    s3 = Step("03_duet_service_info")
    report.steps.append(s3)
    info = (result or {}).get("service_info") if isinstance(result, dict) else None
    if not isinstance(info, dict):
        s3.detail.append("service_info is missing")
    else:
        code_ok = info.get("service_code_hash") == expected_info.get("service_code_hash")
        tools_ok = info.get("service_tools_hashes") == expected_info.get("service_tools_hashes")
        deps_ok = info.get("service_dependencies_hash") in (
            expected_info.get("service_dependencies_hash") or {}).values()
        for ok, what in ((code_ok, "service_code_hash"), (tools_ok, "service_tools_hashes"),
                         (deps_ok, "service_dependencies_hash")):
            if not ok:
                s3.detail.append(f"{what} does not match the manifest")
        s3.ok = code_ok and tools_ok and deps_ok

    return report


def certified_nar_hash(cert: dict, nar_name: str) -> str | None:
    """sha256 (hex) the certificate gives for the output file `nar_name`
    (e.g. '<hash>-<name>.nar'), or None if it does not name that file."""
    try:
        outputs = cert["duet_service_result"]["service_result"]["hashes"]["outputs"]
    except (KeyError, TypeError):
        return None
    value = outputs.get(nar_name) if isinstance(outputs, dict) else None
    return value.lower() if isinstance(value, str) else None


def check_output(cert: dict, nar_name: str, nar_sha256_hex: str) -> Step:
    """Step 05: the NAR received is the one the certificate names."""
    s5 = Step("05_output_hashes")
    want = certified_nar_hash(cert, nar_name)
    if want is None:
        s5.detail.append(f"certificate has no output named {nar_name}")
    elif want != nar_sha256_hex.lower():
        s5.detail.append(f"{nar_name}: NAR sha256 {nar_sha256_hex} != certificate {want}")
    else:
        s5.ok = True
    return s5


def main() -> None:
    """CLI: cactee_verify.py CERT.json MANIFEST.json [NAR_FILE]"""
    import sys
    if len(sys.argv) not in (3, 4):
        raise SystemExit("usage: cactee_verify.py CERT.json MANIFEST.json [path.nar]")
    cert = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    manifest = load_manifest(sys.argv[2])
    simulate = os.environ.get("CACTEE_AZURE_SIMULATE") == "1"
    report = verify_certificate(cert, manifest, simulate_azure=simulate)
    if len(sys.argv) == 4:
        nar = Path(sys.argv[3])
        report.steps.append(check_output(cert, nar.name, sha256_hex(nar.read_bytes())))
    for s in report.steps:
        print(f"{'OK  ' if s.ok else 'FAIL'} {s.name}" + "".join(f"\n       {d}" for d in s.detail))
    print("certificate VALID" if report.ok else "certificate REJECTED")
    raise SystemExit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
