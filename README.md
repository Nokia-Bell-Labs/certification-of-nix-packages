<!--
© 2026 Nokia
Licensed under the MIT License
SPDX-License-Identifier: MIT
-->

# nix-tee

Carrying TEE attestation through the Nix binary cache.

## Quick start: build, deploy, demo

Four containers on one machine: **cache** (Harmonia plus the import API),
**publisher** (runs the orchestrator), **cactee** (the certification service,
simulated) and **client**. The demo package is `mario`: 225 derivations,
certified bottom-up. Details are in `Docker/README.md` (the stack) and
`cactee-pkgs/mario-dep/README.md` (the packages).

**You need** Docker with docker-compose (`docker compose` also works), Nix on
the host (the package generator exports recipes with it), a checkout of
[CACTEE](https://github.com/Nokia-Bell-Labs/confidential-asset-certification-using-tees),
the image `nixtee-publisher:fork` (the Nix fork: upstream Nix at commit `0765ffa5`
plus the patches in `nix-fork/nix-src-patches/`, built once by hand: see "Setup" in
`Docker/README.md`) and, behind a TLS-intercepting proxy, the corporate `.crt` files.

### 1. Build

```sh
cd Docker
cat > .env <<'EOF'
# absolute path of the CACTEE checkout (it contains examples/dev_test_docker)
CACTEE_HOST_PATH=/absolute/path/to/confidential-asset-certification-using-tees
HARMONIA_PUBLISH_TOKEN=nixtee-demo-token
EOF
# behind a proxy: copy the .crt files into cache/ca, node/ca and cactee/ca
docker-compose build
```

The first build is long: Harmonia is compiled from source, with the attestation
patch. Later builds reuse Docker's layer cache, so leave out `--no-cache` unless
you want that compile again. `.env` and the `.crt` files in the `ca/` folders are
not in git.
`docker-compose build` builds `cache` and `cactee` only. `publisher` and `client` run
the `nixtee-publisher:fork` image, which compose does not build. Build it first,
from `nix-fork/`, after copying the `.crt` file to `nix-fork/ca/` (`Docker/README.md`, "Setup"):

```sh
cd ../nix-fork
docker build -t nixtee-publisher:fork .
cd ../Docker
```

That build fetches upstream [NixOS/nix](https://github.com/NixOS/nix) at
`0765ffa540e2adfda7b69efafdbac821ebc48034`, applies `nix-fork/nix-src-patches/`
and compiles the fork. It does not use `nix-fork/nix-src/`.

### 2. Deploy

```sh
docker-compose up -d cactee cache
docker-compose up -d publisher client
curl -sS http://127.0.0.1:5001/health        # ... require_certificate=1 azure_simulate=1
curl -sS http://127.0.0.1:5000/nix-cache-info
curl -sS http://127.0.0.1:8080/health
```

### 3. Demo a package

**Generate the packages** (once, on the host): one folder per derivation, from
the leaves up to `mario`, in `cactee-pkgs/mario-dep/generated/`.

```sh
cd ../cactee-pkgs/mario-dep
GW=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')   # usually 172.17.0.1
python3 generer_dockerfiles.py mario-drv-graph.dot -o generated \
    --ca ../../Docker/node/ca --drv-source https://cache.nixos.org \
    --harmonia "http://$GW:5000" --substituter "http://$GW:5000" --no-require-sigs
```

The generator wants a `zscaler-cert-*.crt` in `Docker/node/ca/`; without a proxy
an empty file with that name is enough.

**Certify and publish** the first three packages: the two leaves
(`bootstrap-tools.tar.xz`, `busybox`) and the first build node. Each one is a
separate CACTEE build.

```sh
docker exec -it -w /mario-dep nixtee-publisher python3 orchestrer_certification.py \
    --harmonia-to http://cache:5000 --stop-on-error --from 1 --to 3
```

For each package CACTEE builds it (a leaf is checked against the hash pinned in
its recipe) and returns a certificate. The cache verifies the certificate, then
checks the NAR against it, and only then registers the package. Success is a
`published <hash>-<name>` line for each output of the package (the third package
has two). Without `--from/--to` the run covers
all 225 packages, one CACTEE build each; `mario` itself (package 225) first needs
its recipe on the host, see "The root recipe" in `cactee-pkgs/mario-dep/README.md`.

**Look at the result:**

```sh
curl -sS http://127.0.0.1:5001/paths                          # the certified store, one path per line
docker logs nixtee-cache 2>&1 | grep -E "nar-import: (certificate|registered)"   # what the cache verified
curl -sS http://127.0.0.1:5000/razasrvdg7ckplfmvdxv4ia3wbayr94s.narinfo   # Cvm* lines next to the usual fields
docker exec nixtee-client nix-store -r /nix/store/razasrvdg7ckplfmvdxv4ia3wbayr94s-bootstrap-tools   # install it from the cache
```

After packages 1 to 3 the store holds four paths: `2pizl7lq4awa7p9bklr8037yh1sca0hg-bootstrap-tools.tar.xz`,
`p9wzypb84a60ymqnhqza17ws0dvlyprg-busybox`, `i9nx0dp1khrgikqr95ryy2jkigr4c5yv-unpack-bootstrap-tools.sh`
and `razasrvdg7ckplfmvdxv4ia3wbayr94s-bootstrap-tools`. They come from the recipes, so they are the same
on every machine. For each one the cache log has a line
`certificate <hash> pending (01_duet_admin_quote=ok 02_controller_signature=ok 03_duet_service_info=ok)`,
then `registered /nix/store/<hash>-<name> sha256:... certified`. The client fetches `bootstrap-tools` and
the `busybox` it references from `http://cache:5000` (the `--add-root` warning is harmless).

**See the cache refuse** a package that comes without a certificate (from
`cactee-pkgs/mario-dep`):

```sh
set -a; source ../../Docker/.env; set +a
printf 'not a real nar' | curl -sS -i -X PUT --data-binary @- \
    -H "Authorization: Bearer $HARMONIA_PUBLISH_TOKEN" \
    -H "X-Nix-Store-Path: /nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab-demo" \
    http://127.0.0.1:5001/import
# HTTP/1.0 412 Precondition Failed
# no verified certificate for aaaa...ab: PUT /attest/aaaa...ab.json before the NAR
```

The other refusals (forged signature, quote of another enclave, other service,
a NAR swapped after certification, Azure token) run without Docker or Nix, from
the repository root, in a few seconds:

```sh
python3 Docker/cache/tests/test_import_api.py
```

## What this is our Goal

### Situation

Install anything from `cache.nixos.org` and you are trusting an infrastructure
you do not control. Hydra, the NixOS build farm, compiles every package in
nixpkgs on machines no outsider inspects, signs each result with the
`cache.nixos.org-1` key, and publishes it. Your client verifies that signature
and installs.

That signature answers one question: *did the holder of this key approve these
bytes?* It does not answer the one that matters for supply-chain integrity :
*where do these bytes come from?* Compromise a build machine and it produces a
backdoored binary, signs it with the real key, and every client accepts it.
Nothing in Nix would detect it — the signature is valid, and it is faithfully
certifying a lie.

The trust boundary sits in the wrong place. A binary is trusted because of
*who built it*, not because of *what it was built from*.

### Goal

A TEE moves that boundary. The package is compiled inside an enclave whose CPU
measures the code being run and signs that measurement with a key burned into
the silicon. Even the administrator of the build machine cannot alter the
result without the measurement changing. The client then verifies something
stronger: this binary came out of this enclave, compiling this exact source.

|  | Question answered | Trust required |
|---|---|---|
| Sig | Who served these bytes? | The key holder and their entire build infrastructure (not trusted) |
| certification | Where do these bytes come from? | The CPU vendor, plus source you can read |

This project makes that certification survive the trip from the builder, through
the cache, to the client. This repository covers the cache.

## Why the cache needs its own work

The attestation cannot ride along with the package, and it cannot be written
into a file ahead of time. Two properties of the existing system force this:

- **`nix copy` cannot carry it.** It transports a package and its full closure
  over the Nix daemon wire protocol, whose field layout is fixed. Adding data
  to it would desynchronise Harmonia from Nix itself.
- **Harmonia has no narinfo files.** Each one is built per request from the
  local store and the Nix SQLite database. There is nothing on disk to rewrite
  after the fact.

What that requires:

- [x] **A second channel** from publisher to binary cache server: a token-gated
  import API next to Harmonia (`:5001`). `PUT /attest/<hash>.json` takes the
  CACTEE certificate first and verifies it (quote, controller signature,
  expected service); `PUT /import` then takes the NAR, checks its sha256
  against the certificate and registers it under its **original** store path,
  with references computed from the NAR. Nothing enters the store without a
  certificate that verifies. Producing a real certificate stays on the builder
  side (CACTEE).
- [x] **A lookup on the cache host**, from a configurable directory
  (`cvm_attestation_dir`), keyed by the same hash the client asks for and read
  in the narinfo handler at request time.
- [x] **A serialisation path** that emits the extra fields without touching the
  formats Harmonia shares with Nix — the attestation is a parameter, not a
  struct field.
- [ ] **Verification on the client**, so that the transported attestation
  carries meaning. **Partialy out of this repository's scope (Client side)**, but the whole point of the
  transport
## The work

### The cache server (the harmonia patch)

`Docker/cache/patches/cvm-attestation-v1.patch` touches three crates:

- `harmonia-cache` — reads the attestation file in the narinfo handler, which
  already holds the validated hash and is allowed to do I/O; adds the
  `cvm_attestation_dir` setting.
- `harmonia-store-path-info` — defines the `CvmAttestation` type.
- `harmonia-store-nar-info` — emits the five `Cvm*` lines next to the existing
  optional fields.

The attestation is passed as a parameter, not stored as a field of struct
`UnkeyedValidPathInfo`. That struct mirrors both the Nix daemon wire protocol
and a versioned JSON format; adding a field there would commit the attestation
to two formats it does not belong to.

### Deploying the cache server

Use the Docker stack under `Docker/` (see `Docker/README.md`): build the cache
image, then `docker-compose up -d cache`. Harmonia is
built from source in `Docker/cache/Dockerfile` with the patch above; the
container serves read `:5000` and token-gated import `:5001`.

### Publishing in the cache server

The publisher sends the certificate of each package to
`PUT :5001/attest/<hashPart>.json` first. The import API verifies it (quote,
controller signature, expected service) and keeps it as pending. The
publisher then sends the NAR to `PUT :5001/import`; the API checks the sha256
of the NAR against the certificate before registering anything, and only then
moves the certificate into the attestation directory as `<hashPart>.json`.
A NAR without a verified certificate is refused (412), a NAR that differs from
what the certificate names is refused (409), a certificate that does not
verify is refused (422). When a client requests
`<hashPart>.narinfo`, Harmonia looks up that file by the same hash and appends
five fields:

```
CvmAttestationType: simulated-v1
CvmBackend: nixtee-cactee
CvmQuote: <base64>
CvmOutputHash: sha256-...
CvmInputHashes: sha256-aaaa sha256-bbbb sha256-cccc
```

Input hashes are sorted, so the narinfo is deterministic regardless of the
order in the source file. A missing or malformed attestation is not an error:
the narinfo is served without the fields, and uncertified packages stay
installable. For a client that requires attestations, that also means a
missing `Cvm*` block must be treated as a refusal, not as an ordinary package
(see the threat model).

The certificates produced today are **simulated** (`Docker/cactee/`): the
CACTEE harness builds the package and the simulator signs a certificate in the
Nokia format with a throw-away key. No TEE is involved: the quote is crafted
and the Azure attestation token is not checked (`CACTEE_AZURE_SIMULATE=1`).
Everything else in the certificate is verified for real.

### The client (the Nix fork patches)

The publisher and client run a fork of Nix. It is upstream
[NixOS/nix](https://github.com/NixOS/nix) at commit
`0765ffa540e2adfda7b69efafdbac821ebc48034` (Nix 2.36.0) with three patches from
`nix-fork/nix-src-patches/`:

- `0001-libstore-cactee-attestation-verification.patch` adds client-side CACTEE
  certificate verification to libstore, plus the `attestation-*` settings,
  `nix-store --import-nar` and serve protocol 2.9. This is the only patch the
  build needs.
- `0002-contrib-attestation-tooling.patch` adds the certifier, signer and
  package-builder scripts under `contrib/attestation/`.
- `0003-contrib-attestation-vendored-sources.patch` adds the third-party sources
  those scripts use (`jo`, busybox, unzip).

`nix-fork/Dockerfile` applies them on top of the upstream commit and installs
the result at `/opt/nix-fork`. `nix-fork/nix-src-patches/README.md` lists every
file the patches add or change, and notes where the code differs from its own
documentation.

**Licence:** the patches are released under the MIT License, but the code they
patch is not all MIT. 0001 modifies Nix, which is LGPL-2.1, and 0003 carries
GPL and Info-ZIP code. See "Licence" at the end of this file.

### Client Request

Once the server is running, from any machine can reach it:

On a client, add the cache as a substituter in `~/.config/nix/nix.conf`:

```
substituters = http://<cache-host>:5000 https://cache.nixos.org
```
```sh
curl http://<cache-host>:5000/nix-cache-info
curl http://<cache-host>:5000/<hashPart>.narinfo
```


## 😨 Iceberg ‼️‼️

The cache side works: an attestation published out-of-band reaches the client
attached to the narinfo. But there is a deep lmitation and it survives everything 
above.

A package is compiled by a toolchain fetched from `cache.nixos.org`: gcc,
binutils, glibc, the whole stdenv. Those come with a signature, not an
attestation. A compromised compiler produces a compromised binary, and the
enclave attests it faithfully — the statement *this binary came out of this
enclave, compiling this source* remains true, and useless. The backdoor is in the
compiler, never in the source.

`CvmInputHashes` records what went in, but a hash identifies an input, it does
not vouch for it.

Two ways out:

- **Attest the closure recursively.** Every dependency carries its own
  attestation, down to a minimal bootstrap compiler that is itself attested.
  This is the only complete answer, and it is a large undertaking — nixpkgs
  holds hundreds of thousands of packages. It is the *scalable certification*
  problem.
- **Accept the boundary and state it.** The attestation covers the final
  build, and the trust chain of its inputs stays open.

This repository now takes the first route for one package, `mario`
(`cactee-pkgs/mario-dep/`): its 225 build derivations are certified one by
one, bottom-up, and each node takes its inputs **only** from the certified
store. The 88 leaves (fixed-output sources, patches, and the two bootstrap
binaries) are checked inside the container against the hash pinned in their
recipe before being certified. What remains at the root is
`bootstrap-tools.tar.xz` and `busybox`: two prebuilt binaries that nothing
builds. The chain shrinks the trust base to them; it does not remove them.


## Threat model

Three trust boundaries:

- **B1 — Publisher to binary cache server.** The import API on `:5001`, over
  plain HTTP with a bearer token: `PUT /attest` for the certificate (verified
  on arrival), `PUT /import` for the NAR (checked against the certificate).
- **B2 — Binary Cache server to client.** Plain HTTP.
- **B3 — The binary cache server itself.** Whoever can write to the store or to the
  attestation directory.

### STRIDE

| | Threat | Status |
|---|---|---|
| **S** | Anyone holding the publish token can publish a NAR and a certificate for any package | **Partly covered**. The token still identifies no producer, but the certificate must verify against the expected enclave and service (MRENCLAVE, controller signature, `service_info`) before the NAR is accepted. With the simulated CACTEE the quote is crafted and the Azure token is not checked |
| **S** | A certificate is attached to the wrong package | **Covered on import**. The certificate must name an output `<hash>-<name>.nar` for the path's own hash, and the sha256 of the received NAR must equal the one it names |
| **T** | A NAR is published under a wrong store path, or with wrong references | **Covered on import**. The path is taken from the request, `NarHash` computed from the bytes, references found by scanning the NAR; a NAR that mentions an uncertified store path is refused |
| **T** | The NAR is altered in transit | **Covered**. `NarHash` in the narinfo, checked by the client after download | — |
| **T** | The narinfo is altered in transit | **Partly covered**. `Sig` binds `StorePath;NarHash;NarSize;References` and nothing else | — |
| **T** | The `Cvm*` fields are altered or forged | **Open.** They fall outside the signed fingerprint, so the cache operator or a man in the middle can replace them entirely |
| **T** | A dependency is backdoored before the build starts | **Open.** Inputs are trusted by signature, not attested |
| **R** | No record of who published which attestation | **Open**. No audit log. The simulated attestation carries the time it was produced, but nothing signs it |
| **I** | An observer learns which packages a client installs | **Open**. Plain HTTP. The contents are public, the inventory may not be |
| **D** | An oversized attestation file is read fully into memory | **Partly covered**. The import API refuses certificates over 512 KiB; Harmonia's `read_to_string` itself still has no cap |
| **E** | A tampered binary runs with the user's privileges | **Open**. This is the impact the whole project exists to prevent |


### What the attestation will still not prove

Even fully verified, and even with an attested toolchain, the attestation
answers *where does this binary come from*, not *is this binary benign*.
Malicious source code compiled inside an enclave produces a valid attestation.

The gain is real but bounded: instead of trusting the holder of a signing key
and their entire build infrastructure, a client trusts the CPU vendor and can
read the source. Reading public source is a tractable problem; auditing
someone else's build farm is not.

## Future work

- **SQLite storage.** The attestation lookup sits behind a single accessor, so
  moving from one file per store path to an indexed table changes nothing else.
  Files are the proof of concept; a table is the production path.
- **Read-side verification in Harmonia.** The import API now refuses
  uncertified imports (`cache/cactee_verify.py`, a Python port of the checks
  in the Nix fork's verifier). Verifying again when Harmonia serves a package
  would also catch a store modified after import, but it only adds protection
  if the NARs and certificates live on a different host than the server that
  serves them: a compromised host could otherwise replace the verifier. A
  Rust implementation, not wired into the build, is in `extras/rust-verify/`.
- **A real TEE.** The simulated CACTEE crafts the quote and skips the Azure
  token (`CACTEE_AZURE_SIMULATE=1`); with the real service the JWT is checked
  against Azure's signing keys and `cactee-manifest.json` must carry the real
  MRENCLAVE.
- **Pinned images.** `Docker/cache` and `Docker/node` install `git`, `curl` and
  `python3` from the current `nixpkgs-unstable`, and `Docker/cactee` installs
  unpinned `pip` packages. Rebuilding the stack later gives different tool
  versions; pinning nixpkgs to a commit would make the images reproducible too.

## Licence

> [!IMPORTANT]
> **This repository, including its patches, is released under the MIT License
> (© 2026 Nokia, `SPDX-License-Identifier: MIT`). The upstream and third-party
> code that the patches modify or contain keeps its own licence, which may
> differ from MIT.**

The full licence text is in [`LICENSE`](LICENSE). The MIT header at the top of
each file covers Nokia's contribution. Applying a patch does not relicense the
code it changes.

Two patches in `nix-fork/nix-src-patches/` include code under other licences:

- **`0001-libstore-cactee-attestation-verification.patch` modifies upstream Nix.**
  22 of its 39 files are existing [NixOS/nix](https://github.com/NixOS/nix)
  files, and they stay under the GNU Lesser General Public License v2.1, as
  Nix does. The files it adds were written for the fork. A built fork combines
  both, so redistributing it is subject to the LGPL-2.1 terms.
- **`0003-contrib-attestation-vendored-sources.patch` contains third-party
  code**, each part under its own licence:

  | Part | Licence |
  |---|---|
  | BusyBox 1.36.1 and its CVE patches | GPL-2.0 |
  | jo 1.7 | GPL-2.0-or-later |
  | Info-ZIP UnZip 6.0 and its CVE patch | Info-ZIP licence |
  | GNU `config.guess` / `config.sub` | GPL-3.0-or-later with the Autoconf exception |

`nix-fork/nix-src-patches/README.md` ("Licence") lists the vendored files one by
one.

The Harmonia patches (`Docker/cache/patches/cvm-attestation-v1.patch`,
`extras/rust-verify/cvm-attestation-v2.patch`) modify
[Harmonia](https://github.com/nix-community/harmonia), which is itself MIT,
© 2022 Helsinki Systems GmbH.