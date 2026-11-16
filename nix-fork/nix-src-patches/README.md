<!--
© 2026 Nokia
Licensed under the MIT License
SPDX-License-Identifier: MIT
-->

# nix-src patches

These patches turn upstream Nix into the fork in [`../nix-src`](../nix-src). [`../Dockerfile`](../Dockerfile) builds the `nixtee-publisher:fork` image from the upstream commit plus these patches.

| | |
|---|---|
| Upstream | https://github.com/NixOS/nix |
| Base commit | [`0765ffa540e2adfda7b69efafdbac821ebc48034`](https://github.com/NixOS/nix/commit/0765ffa540e2adfda7b69efafdbac821ebc48034), `master`, 2026-09-04, "Merge pull request #16340 from JamieMagee/fuzz/seed-replay" (Nix `2.36.0`) |
| Fork branch | `feature/tee-attestation`. Two commits on top of the base: `e4c03b41` "Add libstore attestation checks for Harmonia substitutes" and `25bce136` "Add package-builder overlays, vendor, Docker, and Azure/lab notes" |
| Total | 192 files: 170 added, 22 modified, 0 deleted. 15,623 lines added, 19 removed |

## Licence

> [!IMPORTANT]
> **The patches are released under the MIT License (© 2026 Nokia, `SPDX-License-Identifier: MIT`).
> The upstream and third-party code they modify or contain keeps its own licence, which may differ from MIT.**

The full licence text is in [`LICENSE`](../../LICENSE) at the root of the repository. Every patch file starts with the MIT header. It covers Nokia's contribution: the changes and the new files written for this fork. Applying a patch does not relicense the code it touches. Two of the three patches include code under other licences.

### 0001 modifies upstream Nix (LGPL-2.1)

22 of the 39 files in `0001-libstore-cactee-attestation-verification.patch` are existing Nix files. They are listed under "Changed files" below. Those files, and Nix as a whole, stay under the GNU Lesser General Public License v2.1 ([`COPYING`](https://github.com/NixOS/nix/blob/0765ffa540e2adfda7b69efafdbac821ebc48034/COPYING) in NixOS/nix).

The 17 files 0001 adds were written for the fork, for example `src/libstore/attestation.cc` and `src/certification-verification/`. libstore compiles the certificate verifier in through `cactee-link.cc`, so a built fork is a modified version of Nix that combines MIT and LGPL-2.1 code. Redistributing that build is subject to the LGPL-2.1 terms.

### 0003 contains third-party code

Almost everything in `0003-contrib-attestation-vendored-sources.patch` is vendored upstream source. It keeps the licence its authors gave it:

| Path under `contrib/attestation/package-builder/vendor/` | Upstream | Licence |
|---|---|---|
| `busybox-1.36.1.tar.bz2` | BusyBox 1.36.1 | GPL-2.0-only (`LICENSE` in the tarball) |
| `busybox-patches/*.patch`, `busybox-patches/unsafe_prefix-cve-2026.c` | BusyBox security fixes from BusyBox maintainers and distributions | BusyBox's GPL-2.0. `unsafe_prefix-cve-2026.c` states GPL-2.0-or-later. |
| `jo-src/` | jo 1.7, © 2016 Jan-Piet Mens | GPL-2.0-or-later (`jo-src/COPYING`) |
| `unzip60.tar.gz` | Info-ZIP UnZip 6.0 | Info-ZIP licence (`LICENSE` in the tarball) |
| `unzip-patches/CVE-2021-4217.patch` | UnZip fix by Nils Bars | UnZip's Info-ZIP licence |
| `gnu-config/config.guess`, `gnu-config/config.sub` | GNU config, © Free Software Foundation | GPL-3.0-or-later with the Autoconf exception |

Only `gen-busybox-extra-patches.py` and `busybox-patches/apply-cve-2026.sh` were written for the fork. The MIT header at the top of 0003 does not change the licence of the vendored files.

`0002-contrib-attestation-tooling.patch` adds only new files written for this fork under `contrib/attestation/`. It modifies no upstream file.

## Patches

Apply them in order. Each patch only touches its own set of files, so 0001 can also be applied by itself.

| Patch | Scope | Files | Size | Apply with |
|---|---|---|---|---|
| [`0001-libstore-cactee-attestation-verification.patch`](0001-libstore-cactee-attestation-verification.patch) | The Nix sources: `src/libstore`, `src/nix`, `src/certification-verification`, `doc/manual`. This is all you need to build the fork. | 39 (17 added, 22 modified) | 128 KiB | `git apply` or `patch -p1` |
| [`0002-contrib-attestation-tooling.patch`](0002-contrib-attestation-tooling.patch) | `contrib/attestation/` scripts, docs and package builder, without `vendor/` | 48 added | 136 KiB | `git apply` or `patch -p1` |
| [`0003-contrib-attestation-vendored-sources.patch`](0003-contrib-attestation-vendored-sources.patch) | `contrib/attestation/package-builder/vendor/`: third-party sources, including binary tarballs | 105 added | 5.1 MiB | `git apply` only (it has binary hunks) |

Each patch starts with a short description and a `git diff --stat` summary, followed by a standard `git diff --binary --full-index` body.

### Applying

```bash
git clone https://github.com/NixOS/nix.git && cd nix
```

```bash
git checkout 0765ffa540e2adfda7b69efafdbac821ebc48034
```

```bash
git apply ../nix-src-patches/0001-*.patch ../nix-src-patches/0002-*.patch ../nix-src-patches/0003-*.patch
```

For a Nix `patches = [ ... ]` list, or GNU `patch`, use only 0001 (and 0002 if you need it). Both are text-only.

## How the patches were made

The diff is upstream `0765ffa5` against the contents of `nix-fork/nix-src`. That folder is a Windows checkout copied into this repo, so it carries checkout artifacts that are not real changes. These were left out of the patches:

| Difference in `nix-src` | Count | Why it is excluded |
|---|---|---|
| CRLF line endings | all text files | The Windows checkout converted every file to CRLF. The patches use LF, as upstream does. Upstream's deliberate CRLF test files, such as `tests/functional/lang/eval-fail-eol-3.nix`, are left untouched. |
| Executable bit lost (`100755` → `100644`) | 201 | Checkout artifact. Upstream modes are kept. |
| Symlinks stored as regular files | 99 | Windows checkout without `core.symlinks`. 97 became placeholder files holding the link target, and 2 (`src/nix/help-stores.md`, `src/nix/profiles.md`) became copies of their targets. `contrib/attestation/package-builder/restore-symlinks.sh` exists to undo this. |
| Upstream files missing | 83 | Test data (`*.nar`, `**/ca/`) skipped by `nix-tee/.gitignore`, so it never reached this repo |
| `e -i e4c03b4128c5f5d75f37a0b0a90e8a36c955e935^` | 1 | A 7.6 MB `git log` dump saved by accident, probably from a mistyped `git rebase -i`. It is how the base commit was identified, but it is not source. |

There is one normalization: the 32 added files that start with `#!` are marked executable (`100755`). The Windows checkout lost the original modes, and upstream keeps its scripts executable.

### Verification

- Applying all three patches to a clean checkout of `0765ffa5` produces a git tree identical to the cleaned fork (`3735d2989e9093b9f574cc9d0d29d761984dd241`).
- Every one of the 192 patched files is byte-identical to its counterpart in `nix-fork/nix-src` once CR characters are stripped from text files. Binary files are compared as they are.
- GNU `patch` 2.7.6 applies 0001 and 0002 cleanly and sets the executable bits.
- `git apply` warns about trailing whitespace. That whitespace is in the fork's own files and in the vendored CVE patches, so it is kept on purpose.
- The fork was not compiled as part of this check.

## Changed files (22, all in 0001)

| File | +/− | Change |
|---|---|---|
| `src/libstore/store-api.cc` | +37 −5 | Adds a default `Store::addAttestationSidecar()`, which only verifies. `copyStorePath()` now copies the certificate even when the destination already has the path. After `addToStore` it copies and verifies the certificate, and on failure invalidates the path again (`LocalStore` only). `copyPaths()` copies certificates before `addMultipleToStore`, and also copies them for paths that are already valid. |
| `src/libstore/include/nix/store/store-api.hh` | +9 | New virtual `addAttestationSidecar(path, narHash, json, sig)` |
| `src/libstore/local-store.cc` | +11 −2 | `LocalStore::addAttestationSidecar()` verifies the certificate and saves it under `attestation-store`. The two `#embed "*.sql"` lines become `#include "*.sql.gen.hh"`, because the pinned compilers lack `#embed` (GCC < 15, Clang < 19). |
| `src/libstore/include/nix/store/local-store.hh` | +8 −5 | `addAttestationSidecar` override. `invalidatePathChecked()` moves from private to public so `copyStorePath()` can roll back. |
| `src/libstore/binary-cache-store.cc` | +9 | `BinaryCacheStore::addAttestationSidecar()` verifies only and never uploads to the cache |
| `src/libstore/include/nix/store/binary-cache-store.hh` | +3 | Override declaration |
| `src/libstore/legacy-ssh-store.cc` | +8 | `LegacySSHStore::addAttestationSidecar()` verifies on the client. It does not send `PutAttestation` to the remote. |
| `src/libstore/include/nix/store/legacy-ssh-store.hh` | +3 | Override declaration |
| `src/libstore/include/nix/store/serve-protocol.hh` | +4 −2 | Serve protocol bumped from 2.8 to 2.9, with a new command `PutAttestation = 10` |
| `src/libstore/include/nix/store/globals.hh` | +16 −1 | `Settings` also inherits `AttestationSettings`. Adds `getAttestationSettings()`. |
| `src/libstore/globals.cc` | +2 | `AttestationSettings::anchor()` |
| `src/libstore/include/nix/store/worker-settings.hh` | +4 −2 | **The default `substituters` is now empty** instead of `https://cache.nixos.org/`. The doc text is updated to match. |
| `src/libstore/include/nix/store/build/worker.hh` | +3 | `Worker::attestationPreflightDone` (`StorePathSet`). Nothing uses it yet. |
| `src/libstore/build/derivation-building-goal.cc` | +1 | `#include "nix/store/attestation.hh"` only |
| `src/libstore/build/substitution-goal.cc` | +2 | `#include`s only. Substitution reaches the new checks through `copyStorePath()`. |
| `src/libstore/meson.build` | +27 | New `libcrypto`/`openssl` dependency. `HAVE_EMBEDDED_SANDBOX_SHELL` is always defined, because `-Werror=undef` rejects it otherwise. The SQL schemas are wrapped with `generate-header`. Adds sources `attestation.cc` and `cactee-link.cc`. |
| `src/libstore/include/nix/store/meson.build` | +2 | Installs `attestation-settings.hh` and `attestation.hh` |
| `src/libstore/package.nix` | +4 | `openssl` input. The fileset gains `../certification-verification/certificate-verifier.{cc,hh}`. |
| `src/nix/nix-store/nix-store.cc` | +101 −2 | New `nix-store --import-nar <path> <nar>` with `--nar-hash`, `--references`, `--deriver`, `--attestation-json` and `--attestation-sig`. `nix-store --serve` handles `PutAttestation`, which needs protocol 2.9 and `--write`. Flag parsing keeps `-q --references` working. Two lines change only in whitespace. |
| `doc/manual/source/SUMMARY.md.in` | +1 | Links `nix-store --import-nar` |
| `doc/manual/source/command-ref/nix-store.md` | +1 | Links `--import-nar` |
| `doc/manual/source/command-ref/nix-copy-closure.md` | +5 | Notes that the fork copies `{hashPart}.attestation.json`/`.sig` before the NAR |

## Added files

### 0001: Nix sources (17)

| File | Lines | Purpose |
|---|---|---|
| `src/libstore/attestation.cc` | 557 | Core logic. Finds a path's certificate in the binary cache (`GET {hashPart}.attestation.json`), in local sidecars, or in `/var/lib/nix-certification/{hashPart}.json`. It requires a CACTEE certificate, runs the verifier against `attestation-cactee-manifest` and the NAR SHA-256, exempts `attestation-bootstrap-inputs`, saves the certificate under `attestation-store`, and walks narinfo closures leaves-first. |
| `src/libstore/include/nix/store/attestation.hh` | 64 | Public API: `verifyPathAttestation`, `copyAttestationSidecar`, `verifyIncomingAttestation`, `installAttestationSidecar`, `bindAttestationSidecarFromDisk`, `verifyDirectInputAttestations`, `verifyPathClosureAttestations`, `requireIncomingAttestationSidecar`, `attestationCertPath` |
| `src/libstore/include/nix/store/attestation-settings.hh` | 72 | New `nix.conf` settings: `require-attestation` (default **true**), `attestation-store` (default `/var/lib/nix-attestations`), `attestation-bootstrap-inputs`, `attestation-cactee-manifest` |
| `src/libstore/cactee-link.cc` | 3 | `#include "../certification-verification/certificate-verifier.cc"`, which compiles the verifier into libstore |
| `doc/manual/source/command-ref/nix-store/import-nar.md` | 44 | Manual page for `nix-store --import-nar` |
| `src/certification-verification/certificate-verifier.cc` | 831 | CACTEE certificate verifier (OpenSSL + libcurl). Step `01_duet_admin_quote` checks the duet admin SGX quote with Azure Attestation and compares the MRENCLAVE with the manifest. Then come `02_controller_signature`, `03_duet_service_info` (service info against the manifest) and `05_output_hashes` (output/NAR hashes). `CACTEE_AZURE_SIMULATE=1` switches to a local Azure stand-in. |
| `src/certification-verification/Include/certificate-verifier.hh` | 38 | `certificate_verifier::CertificateVerifier`, `VerificationResult`, `StepResult` |
| `src/certification-verification/main.cc` | 32 | Standalone CLI: `verify <certificate.json> <manifest.json> <output-folder>` |
| `src/certification-verification/meson.build` | 16 | Standalone Meson project for the `verify` executable. The main Nix build does not use it. |
| `src/certification-verification/smoke-test.cc` | 61 | Smoke test for the verifier |
| `src/certification-verification/sample-certificate.json` | 130 | Sample CACTEE asset certificate: SGX quote, public key, service result |
| `src/certification-verification/simulate-azure.py` | 159 | Local stand-in for Azure login, SGX attestation and `/certs` JWKS (used with `CACTEE_AZURE_SIMULATE=1`) |
| `src/certification-verification/smoke.sh` | 10 | Builds and runs the smoke test in a `nix shell` |
| `src/certification-verification/run-smoke.sh` | 4 | Builds and runs the smoke test in a `nix-shell` |
| `src/certification-verification/check-nix-settings.sh` | 8 | Prints which `nix` is used and its `attestation*` settings |
| `src/certification-verification/which-nix.sh` | 15 | Looks for fork `nix` binaries in developer home directories |
| `src/certification-verification/copy-to-wsl.sh` | 25 | Copies verifier sources from a Windows path into WSL (developer helper with hard-coded paths) |

### 0002: `contrib/attestation/` tooling (48)

**Documentation**

| File | Lines | Purpose |
|---|---|---|
| `README.md` | 107 | Overview of Nix TEE attestation and the "binding record v1" certificate format |
| `SETUP.md` | 67 | Architecture and setup: post-build-hook → certificate → input checks |
| `AZURE.md` | 71 | Reproducing the attested `jo` build and client verification on an Azure/lab VM |
| `TODAY.md` | 84 | Lab notes from 2026-09-14: builder → Harmonia → client substitution issues |
| `DEMO.md` | 5 | Demo pointers |
| `SPEAKER-SCRIPT.md` | 24 | Speaker notes for the 10-slide demo |
| `slides.html` | 190 | Demo slide deck |

**Certificate tooling**

| File | Lines | Purpose |
|---|---|---|
| `nix-certifier` | 147 | Python post-build hook that writes binding record v1 certificates for each output (`DRV_PATH`, `OUT_PATHS`) |
| `nix-attestation-verify` | 124 | External verifier: `nix-attestation-verify <certificate.json>`, exit 0 if valid |
| `nix-attestation-backfill` | 168 | Creates certificates for a closure that was built before attestation was turned on |
| `binding.py` | 95 | Shared binding record v1 computation |
| `sign-attestation-certs.py` | 104 | Signs certificate JSON with a Nix binary-cache Ed25519 key and writes `<hashPart>.attestation.sig` |
| `ingest-nars-to-store.py` | 174 | Registers every NAR of an export tree leaves-first with `nix-store --import-nar`, binding cert and sig |
| `push-exports-to-harmonia.py` / `.sh` | 13 / 5 | Deprecated stub that points to `ingest-nars-to-store.py` + `nix-copy-closure` |
| `fix-wsl-symlinks.py` | 29 | Restores symlinks flattened by a Windows checkout (WSL variant) |

**`package-builder/`**: Docker pipeline that compiles the fork and builds and exports an attested `jo` closure

| File | Lines | Purpose |
|---|---|---|
| `README.md` | 62 | Commands used on the Ubuntu 24.04 builder |
| `Dockerfile` | 102 | Two stages: compile the Nix fork (`substitute = false`), then build `jo` and export NARs |
| `build.sh` | 57 | BuildKit build wrapper: corporate CA, fork source path, log saving |
| `compile-nix-fork.sh` | 30 | Builds the fork with classic `nix-build` |
| `nix-fork-classic.nix` | 121 | Builds the fork without flakes, on nixpkgs 26.05 |
| `fork-build-overlay.nix` | 57 | Overlays needed behind the proxy for the fork build |
| `fork-nixpkgs/default.nix` | 11 | nixpkgs wrapper with the overlays applied |
| `gnu-mirror-overlay.nix` | 146 | Moves GNU sources to working mirrors and uses the vendored `gnu-config` |
| `nixpkgs-with-overlay.nix` | 5 | Imports `/nixpkgs` with the mirror overlay |
| `jo.nix` | 31 | `jo` 1.7 from `vendor/jo-src` |
| `nix-docker.conf` | 26 | `nix.conf` for the build containers |
| `export-closure.sh` | 412 | Exports the `jo` derivation graph leaves-first, one NAR per node where possible |
| `prefetch.sh` | 461 | Prefetch logic: bootstrap seeds, busybox, `jo` sources |
| `gen-package-nix.sh` | 51 | Generates `packages/*.nix` from `runtime-attrs.txt` |
| `packages/{bash,bash-completion,gcc-cc-lib,glibc,ncurses,readline,tzdata}.nix` | 4 each | Generated one-line package expressions |
| `packages/runtime-attrs.txt` | 15 | `jo`'s runtime dependencies, bottom-up |
| `packages/build-order.txt` | 7 | Build order |
| `restore-symlinks.sh` | 47 | Recreates the symlinks flattened by the Windows checkout (run in the container) |
| `fix-eol.py` | 37 | Forces LF on every file the Linux build reads |
| `setup-ca-bundle.sh` | 48 | Builds `/etc/nix/ca-bundle.crt`, with an optional corporate CA |
| `sync-nix-cache.sh` | 47 | Copies `/nix` into or out of a BuildKit cache mount |
| `stage-nokia-runner.sh` | 51 | Stages the build context for the Nokia cert runner |
| `nokia-runner/Dockerfile` | 57 | Cert runner image: one `nix-build` per runtime package, then `jo` |
| `nokia-runner/.gitignore` | 4 | Ignores staged files |
| `.gitignore` | 9 | Keeps keys out of git (`cache.secret`, `*.pem`, `certs/*.key`, …) |
| `certs/.gitkeep` | 1 | Placeholder |

### 0003: vendored third-party sources (105)

All paths are under `contrib/attestation/package-builder/vendor/`.

| Path | Files | Content |
|---|---|---|
| `busybox-1.36.1.tar.bz2` | 1 | BusyBox 1.36.1 release tarball, binary, SHA-256 `b8cc24c9574d809e7279c3be349795c5d5ceb6fdf19ca709f80cde50e47de314` |
| `busybox-patches/` | 9 | `CVE-2022-48174.patch`, `CVE-2023-39810.patch`, `CVE-2023-42363.patch`, `CVE-2023-42364_CVE-2023-42365.patch`, `CVE-2023-42366.patch`, `CVE-2026-26157_CVE-2026-26158.patch`, `CVE-2026-26157_CVE-2026-26158-2.patch`, plus `apply-cve-2026.sh` and `unsafe_prefix-cve-2026.c` for the 2026 tar fixes |
| `gen-busybox-extra-patches.py` | 1 | Generates the CVE-2023-42363 patch and checks the Alpine 42364 patch |
| `unzip60.tar.gz` | 1 | Info-ZIP UnZip 6.0 tarball, binary, SHA-256 `036d96991646d0449ed0aa952e4fbe21b476ce994abc276e49d30e686708bd37` |
| `unzip-patches/CVE-2021-4217.patch` | 1 | UnZip null-pointer fix |
| `gnu-config/config.guess`, `config.sub` | 2 | GNU `config` scripts, vendored because Savannah cgit is unreachable through the proxy |
| `jo-src/` | 90 | `jo` 1.7 source tree (5,398 lines, plus `tests/jo-logo.png`): 23 top-level files (`jo.c`, `json.c/h`, `base64.c/h`, `configure.ac`, `Makefile.am`, `meson.build`, man page, completions, packaging files, …), `build-aux/tap-driver.sh`, `debian/` (9), `rpm-build/jo.spec`, and `tests/` (56: `jo.01`–`jo.27` `.sh`/`.exp` pairs, `jo.test`, `jo-creator.txt`, `jo-logo.png`) |

## Observations

These came up while documenting the files. They describe the code as it is in this snapshot.

1. **`.sig` files are not checked.** `verifyIncomingAttestation()` takes the signature but ignores it (`(void) sig`). The `.sig` files are found but never checked against `trusted-public-keys`. In this snapshot, trust rests entirely on the CACTEE verifier. `import-nar.md` and `sign-attestation-certs.py` describe a signature check that does not happen.
2. **Build-input pre-flight is not wired in.** Nothing calls `verifyDirectInputAttestations()`, `requireIncomingAttestationSidecar()` or `Worker::attestationPreflightDone`. The two build-goal files only gain `#include`s. Enforcement happens on copy and substitution (`copyStorePath`/`copyPaths`), not before a build starts, despite what `contrib/attestation/README.md` and the `require-attestation` doc say.
3. **`PutAttestation` is server-side only.** `nix-store --serve` handles it, but no client sends it: `LegacySSHStore::addAttestationSidecar()` verifies locally. The `nix-copy-closure.md` and `import-nar.md` doc text, which says sidecars are sent before the NAR over serve 2.9, does not match this.
4. **The NAR is written before the check during substitution.** `copyStorePath()` adds the NAR and then verifies the certificate, rolling back with `invalidatePathChecked()` on failure. The verifier needs the realised path to hash it. The `require-attestation` doc says the NAR "is not written until" verification.
5. **A hard-coded fallback directory.** `copyAttestationSidecar()` also reads `/var/lib/nix-certification/{hashPart}.json` (the Harmonia drop directory) when no other certificate is found.
6. **Default behaviour changes.** `require-attestation = true` and an empty `substituters` list are new defaults. A stock configuration will therefore refuse uncertified paths and no longer uses cache.nixos.org.
