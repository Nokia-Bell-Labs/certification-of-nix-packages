<!--
© 2026 Nokia
Licensed under the MIT License
SPDX-License-Identifier: MIT
-->

# Rust variant: Harmonia verifies the CACTEE certificate itself

Kept for the record. The demo in the root `README.md` does not use it, and it is
not wired into `Docker/cache/Dockerfile`.

## What it is

`cvm-attestation-v2.patch` replaces `Docker/cache/patches/cvm-attestation-v1.patch`.
It contains v1 (the `Cvm*` lines in the narinfo) and, on top of it:

- a new crate, `harmonia-cactee-verify`: the checks of the Nix fork's verifier
  (`certificate-verifier.cc`) and of `Docker/cache/cactee_verify.py`, with the same
  step names: `01` SGX quote and Azure attestation token, `02` controller signature,
  `03` service info against the manifest, `05` output hash against the NAR;
- `harmonia-cache/src/cvm.rs`: before a narinfo is served, the certificate
  `<cvm_attestation_dir>/<hash>.json` is verified against the manifest and against the
  narHash in the Nix database, which is the sha256 of the NAR this server streams.
  With `cvm_require_certificate = true` a path without a verified certificate gets a
  404 (`not certified: <reason>`). Results are cached until the certificate file changes.

## How it differs from branch `Harmonia`

On `Harmonia` the check happens when a package is admitted: the import API
(`Docker/cache/nar-import-server.py`, Python) verifies the certificate and the NAR
before anything enters the store. Harmonia itself trusts the store and only prints the
`Cvm*` lines. This variant verifies inside Harmonia, when the narinfo is served, so a
path that reached the store by another route, or a NAR replaced afterwards, is not served
as certified. The two can run together.


## To wire it in (three changes to the tree on `Harmonia`)

1. `Docker/cache/Dockerfile`: copy and apply `patches/cvm-attestation-v2.patch` instead of
   v1, and check that `cvm_require_certificate` is in `config.rs` and in the binary.
2. `Docker/cache/entrypoint.sh`: write to `/etc/harmonia.toml`
   `cvm_manifest = "/etc/nixtee/cactee-manifest.json"`, `cvm_require_certificate = true` and
   `cvm_azure_simulate = true` (simulated CACTEE only; for a real one set `cvm_azure_jwks`).
3. `Docker/docker-compose.yml`, service `cache`: pass `CVM_REQUIRE_CERTIFICATE` and
   `CVM_AZURE_SIMULATE`.

Then `docker-compose build cache`. Harmonia is compiled again.

## Open point, both variants: the Azure attestation token

The Nix fork's verifier obtains the token itself: it logs in to Azure AD
(`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`) and posts the quote to the
attestation service. The cache verifiers (Python and Rust) do not call Azure. They expect
the token inside the certificate, as `duet_admin_quote.jwt`, and check it against the
service's published keys (issuer, MRENCLAVE, report data, quote hash). Nokia's sample
certificate carries no `jwt`, so this check has not been run on a real certificate. The
demo skips it (`CACTEE_AZURE_SIMULATE=1`, `cvm_azure_simulate = true`).
