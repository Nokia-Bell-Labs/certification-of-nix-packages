<!--
© 2026 Nokia
Licensed under the MIT License
SPDX-License-Identifier: MIT
-->

# Four-container simulation

Short path to a running demo (build, deploy, demo a package): the root
`README.md`, "Quick start". This file has the details.

```
publisher  --POST tar(config+code+inputs)-->  cactee:8080
publisher  --PUT cert, then PUT NAR (token)-->  cache:5001  --verifies, writes /certified_store-->  cache:5000  <--GET--  client
```

Harmonia (`:5000`) and the import API (`:5001`) run in **one** container.

| Container | Hostname | Role |
|---|---|---|
| `nixtee-cache` | cache | Harmonia GET `:5000` + token PUT `:5001`; **verifies every CACTEE certificate before a package enters the store** |
| `nixtee-publisher` | publisher | `/mario-dep` tree; PUTs certificate, then NAR; POSTs to cactee |
| `nixtee-cactee` | cactee | harness `:8080`; empties the harness inputs folder, then runs `test_docker.py`, which builds every package through the host's docker.sock; answers with Nokia-format (simulated) certificates |
| `nixtee-client` | client | substituter `http://cache:5000` only |

Publisher does **not** mount docker.sock. CACTEE does **not** mount mario-dep.

## Layout

```
Docker/
├── docker-compose.yml
├── .env                    # HARMONIA_PUBLISH_TOKEN, CACTEE_HOST_PATH
├── cache/                  Harmonia image + nar-import-server.py + cactee_verify.py + cactee-manifest.json
├── node/                   publisher + client image (publish helper)
└── cactee/                 POST /certify
```

## Setup

`nixtee-publisher:fork` is built by hand from `nix-fork/`. Compose does not build it. Publisher and client both use that tag. The image is `nixos/nix:2.31.2` plus the fork installed at `/opt/nix-fork`, with `require-attestation = false`.

The fork is built from upstream, not from `nix-fork/nix-src/`. `nix-fork/Dockerfile` does three things:

1. Fetches [NixOS/nix](https://github.com/NixOS/nix) at commit `0765ffa540e2adfda7b69efafdbac821ebc48034` (Nix 2.36.0) into `/nix-src`.
2. Applies the patches in `nix-fork/nix-src-patches/` in order: `0001` libstore/`nix-store` attestation verification, `0002` `contrib/attestation` tooling, `0003` vendored sources.
3. Compiles the result with Meson.

`nix-fork/.dockerignore` keeps `nix-src/` out of the build context. The build must reach `github.com`; behind the proxy that needs the CA file below. `nix-fork/nix-src-patches/README.md` lists every file the patches add or change.

Two build arguments select the source. `NIX_REPO` (default `https://github.com/NixOS/nix.git`) can point at a mirror. `NIX_REV` must be a full 40-character commit hash, and the patches only apply to `0765ffa5`.

A corporate CA file is required in every image that downloads during `docker build`: the fork image, the Harmonia cache image, and the certifier image. The certificate stays out of git. From `nix-fork/`, with `CORP_CA` set to that file:

```sh
mkdir -p ca
cp "$CORP_CA" ca/host-ca.crt
cp "$CORP_CA" ../Docker/node/ca/host-ca.crt
cp "$CORP_CA" ../Docker/cactee/ca/host-ca.crt
cp "$CORP_CA" ../Docker/cache/ca/host-ca.crt
docker build -t nixtee-publisher:fork .
```

`.env` is not in git. Compose reads it from `Docker/`. `CACTEE_HOST_PATH` is the absolute path of the cactee checkout (`examples/dev_test_docker` must be inside it). It is mounted at that same path in `nixtee-cactee`. `HARMONIA_PUBLISH_TOKEN` is the bearer token for port 5001. Compose uses `nixtee-demo-token` when the variable is unset. From `Docker/`:

```sh
cat > .env << EOF
CACTEE_HOST_PATH=/absolute/path/to/cactee
HARMONIA_PUBLISH_TOKEN=nixtee-demo-token
EOF

chmod +x node/node-init.sh
docker compose up -d --build
```

`docker-cache` and `docker-cactee` are built here. `nixtee-publisher` and `nixtee-client` use the tag built above. Verification on the client is on because `VERIFY_ATTESTATION=1` is set in Compose.

## Generate the package Dockerfiles

From `cactee-pkgs/mario-dep`, on the host. The generator needs Nix on the host: it fetches each leaf's closure and each node's recipe from `https://cache.nixos.org` and writes them to the package's `inputs/` folder. That folder is the one CACTEE input every package must have, and the orchestrator refuses a package without it. Do not pass `--no-bundle-leaves`: it writes leaves without that input, and the orchestrator stops with `expected exactly one CACTEE input (closure export), found 0`.

The generator only accepts a certificate named `zscaler-cert-*.crt`. Copy `host-ca.crt` to that name in `Docker/node/ca/` first. The generated Dockerfiles start `FROM nixtee-publisher:fork`, so build that image first (see Setup). `$GW` is the host bridge gateway. The name `cache` exists only on the Compose network, and these builds run through the host Docker daemon.

```sh
cp ../../Docker/node/ca/host-ca.crt ../../Docker/node/ca/zscaler-cert-host.crt

GW=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')

python3 generer_dockerfiles.py mario-drv-graph.dot \
    -o generated \
    --ca ../../Docker/node/ca \
    --drv-source https://cache.nixos.org \
    --harmonia "http://$GW:5000" \
    --substituter "http://$GW:5000" \
    --no-require-sigs
```

Package 225 is `mario` itself. Its recipe is on no public cache, so the generator skips it and says so until the recipe has been imported on the host: see "The root recipe" in `cactee-pkgs/mario-dep/README.md`. The other 224 packages do not need it.

## Certify and publish

From `cactee-pkgs/mario-dep`. The orchestrator does not read `.env` by itself. Load it in the same shell, or the cache answers HTTP 401. Ports 8080 and 5000 are published on the host, so the URLs are `127.0.0.1`.

```sh
set -a
source ../../Docker/.env
set +a
python3 orchestrer_certification.py \
    --cactee-url http://127.0.0.1:8080 \
    --harmonia-to http://127.0.0.1:5000
```

The run walks `generated/00-build-order.txt`: 88 leaves, then 137 nodes, 225 packages. CACTEE runs each Dockerfile inside `nixtee-publisher:fork`. It does not build a new image. A package already on Harmonia is skipped.

For each package the orchestrator prints `[NNN] leaf` or `[NNN] node` and sends the package to CACTEE, which returns the NAR and a simulated certificate (several minutes per package). A node must really be built: its CACTEE log has to show `building '...'`. If the output was copied instead, the run fails with `no 'building' line in the log`. The orchestrator then PUTs the certificate to `/attest/<hash>.json` on port 5001, and the NAR to `/import`. Success for one package is `published <store-name>`, once per output. The cache answers 409 when a NAR names a dependency that is not certified yet, so the leaves go first. This pass takes a long time. Leave it running until the final `done` / `skipped` / `failed` line.

## Reset and rebuild

```sh
cd Docker
docker rm -f nixtee-cache nixtee-cactee nixtee-publisher nixtee-client 2>/dev/null || true
docker-compose down -v --remove-orphans

export COMPOSE_HTTP_TIMEOUT=300
docker-compose build --no-cache
docker-compose up -d cactee cache
docker-compose up -d publisher client
```

`--no-cache` also compiles Harmonia again from source. After a code change,
`docker-compose build` alone rebuilds only the layers that changed.

If cache recreate fails with `KeyError: 'ContainerConfig'` (compose 1.29):

```sh
docker rm -f nixtee-cache
docker-compose up -d cache
```

Health:

```sh
docker ps --filter name=nixtee
curl -sS http://127.0.0.1:5001/health      # ... require_certificate=1 azure_simulate=1
curl -sS http://127.0.0.1:5000/nix-cache-info
curl -sS http://127.0.0.1:8080/health
```

Use **`docker-compose`** (hyphen) if `docker compose` errors with `unknown shorthand flag: 'd'`.

## Orchestrator (mario-dep)

Inside the publisher (`CACTEE_URL` is `http://cactee:8080`, import uses `http://cache:5001`):

```sh
docker exec -it -w /mario-dep nixtee-publisher python3 orchestrer_certification.py \
  --harmonia-to http://cache:5000 --stop-on-error
```

Smoke (first three packages):

```sh
docker exec -it -w /mario-dep nixtee-publisher python3 orchestrer_certification.py \
  --harmonia-to http://cache:5000 --stop-on-error --from 1 --to 3
```

From the host, in `cactee-pkgs/mario-dep`, load the token and then certify. This is the same command as in Setup:

```sh
set -a
source ../../Docker/.env
set +a
python3 orchestrer_certification.py \
    --cactee-url http://127.0.0.1:8080 \
    --harmonia-to http://127.0.0.1:5000
```

After `docker-compose down -v`, the certified store is empty; the orchestrator certifies and publishes everything again. With an existing cache volume, packages already on Harmonia are skipped (looked up by exact store path).

The Dockerfiles under `generated/` are built by the **host's** Docker, outside
the compose network, so they cannot use `http://cache:5000`. Generate them with
the bridge gateway address instead (usually `http://172.17.0.1:5000`); see
`cactee-pkgs/mario-dep/README.md`. The orchestrator itself runs in the
publisher and keeps using `http://cache:5000`.

## The import API (`:5001`)

Nothing enters the certified store without a CACTEE certificate that verifies
(`REQUIRE_CERTIFICATE=1`, the default). Two requests per package, **in this
order**:

1. `PUT /attest/<hash>.json` with the CACTEE certificate as body. The server
   checks everything that needs only the certificate (`cache/cactee_verify.py`,
   the same steps as the verifier in the Nix fork) and keeps it as *pending*:
   - `01` SGX quote: MRENCLAVE equals the one in `cache/cactee-manifest.json`,
     `report_data` is the sha256 of the controller's public key, and the Azure
     attestation token vouches for the quote (skipped with
     `CACTEE_AZURE_SIMULATE=1`, set in `docker-compose.yml` because the
     simulated CACTEE has no Azure token)
   - `02` the controller's RSA-PSS signature over the service result
   - `03` `service_info` is the expected service (code hash, dependencies,
     tools) from the manifest

   A failing step answers **422** and is named in the message.
2. `PUT /import` with `X-Nix-Store-Path: /nix/store/<hash>-<name>` and the NAR as
   body. The server:
   - computes `NarHash` and `NarSize` from the bytes it receives
   - **refuses with 412** if no verified certificate is pending for that hash
   - **refuses with 409** if the sha256 of the NAR is not the one the certificate
     names for `<hash>-<name>.nar` (step `05`). This happens before anything is
     written to the store
   - finds the references by scanning the NAR for the hashes of paths already in
     the certified store (the same rule Nix uses after a build), plus its own
   - **refuses with 409** a NAR that mentions `/nix/store/<hash>-` for a path that
     is not certified yet: publishing it would create an incomplete closure.
     The all-`a` hash that bootstrap GCC writes before the real libc path exists
     is a placeholder and is ignored
   - restores it at **that exact path** and registers it (`nix-store --load-db`)
   - moves the certificate to `<hash>.json`, where Harmonia reads it for the
     `Cvm*` narinfo fields
   - accepts a re-import of identical content, refuses a different one (409)

If the path is already in the store when its certificate arrives (a shared
source), `/attest` checks the NAR hash against the stored one immediately.

`GET /paths` lists the certified store. `GET /health` shows the active settings.

The manifest holds the MRENCLAVE and service hashes the cache trusts. The
simulated CACTEE in `docker-compose.yml` signs with the same values
(`CACTEE_MRENCLAVE`, `CACTEE_SERVICE_*`): change both together, or every
certificate is refused at step `01`/`03`. The client has a third copy: the
manifest that `node/node-init.sh` writes when `CACTEE_AZURE_SIMULATE=1`. Keep all
three equal. `REQUIRE_CERTIFICATE=0` (development only) lets uncertified NARs in;
the log then says `UNCERTIFIED`.

The checks can be run without Docker or Nix (30 cases: no certificate, a NAR
swapped after certification, forged signature, quote of another enclave, other
service, Azure token, placeholder hash, and the simulated CACTEE's own
`/certify`). `nix-store` is replaced by a stand-in; needs `openssl` and
`pip install cryptography`. From the repository root:

```sh
python3 Docker/cache/tests/test_import_api.py       # add -v for the server log
```

Check a certificate by hand, outside the cache (the NAR file must be named
`<hash>-<name>.nar`):

```sh
CACTEE_AZURE_SIMULATE=1 python3 cache/cactee_verify.py cert.json cache/cactee-manifest.json [<hash>-<name>.nar]
```

## Manual publish (optional)

```sh
docker exec nixtee-publisher /usr/local/bin/publish /nix/store/<hash>-<name> [certificate.json]
```

`publish` PUTs the certificate to `http://cache:5001/attest/<hash>.json`, then
the NAR to `/import` (see `node/publish.sh`). Without a certificate the NAR is
refused with 412. Call it by its full path: `/usr/local/bin` is not on the
`PATH` of the `nixos/nix` image. It publishes one path; its references must
already be in the certified store, or the import answers 409 with the missing
hashes. Each path needs its own certificate, so a whole closure is published by
the orchestrator, bottom-up, not by a shell loop. For a quick test without
certificates, start the cache with `REQUIRE_CERTIFICATE=0`:

```sh
docker exec nixtee-publisher sh -c \
  'for p in $(nix-store -q --requisites <path>); do /usr/local/bin/publish $p; done'
```

## Remote hosts

Set `HARMONIA_PUBLISH_TOKEN` in `.env` or the environment. Users need:

- read: `http://HOST:5000`
- publish: `http://HOST:5001` with the same token

Orchestrator from another machine:

```sh
python3 orchestrer_certification.py --stop-on-error \
  --cactee-url http://HOST:8080 \
  --harmonia-to http://HOST:5000
```

## Client verification

Run this after the orchestrator has published Mario (`published <mario-output>`). For this package the output is `/nix/store/1abb34kvsvwbx70v145jacvdl0qnhps4-mario-1.0`. The client is `nixtee-client`, the same fork image, with `VERIFY_ATTESTATION=1` and `CACTEE_AZURE_SIMULATE=1`. `unset NIX_REMOTE` keeps the command in this binary. With `NIX_REMOTE` set, the request goes to the stock daemon and skips the certificate check.

```sh
docker exec nixtee-client bash -lc \
  'unset NIX_REMOTE; nix-store --realise /nix/store/1abb34kvsvwbx70v145jacvdl0qnhps4-mario-1.0'
```

A run that actually checks the certificate logs:

```text
copying path '/nix/store/1abb34kvsvwbx70v145jacvdl0qnhps4-mario-1.0' from 'http://cache:5000'...
attestation: read certificate for '/nix/store/1abb34kvsvwbx70v145jacvdl0qnhps4-mario-1.0' from Harmonia directory /var/lib/nix-certification/1abb34kvsvwbx70v145jacvdl0qnhps4.json
attestation: certificate 1abb34kvsvwbx70v145jacvdl0qnhps4-mario-1.0.nar sha256 is 0c9158ae3ad8dd37c4418040514437640fc71e731306642f25f344e21141b446 (match)
attestation: 01_duet_admin_quote ok — azure attestation accepted the quote (simulation) public key hash matches quote report_data mrenclave matches
attestation: 02_controller_signature ok — signature matches
attestation: 03_duet_service_info ok — service info matches
attestation: 05_output_hashes ok — NAR sha256 matches the certificate
attestation: JSON certificate verified for '/nix/store/1abb34kvsvwbx70v145jacvdl0qnhps4-mario-1.0' (narHash sha256-DJFYrjrY3TfEQYBAUUQ3ZA/HHnMTBmQvJfNE4hFBtEY=)
```

The four steps are printed twice: once while the certificate is copied in, and once by the check after the NAR is stored. `JSON certificate verified` is the success line. Quote text says simulation because `CACTEE_AZURE_SIMULATE=1`. The `--add-root` warning does not affect the check.

`nix-store --realise` creates a substitution goal for that path. Harmonia is the substituter `http://cache:5000`.

1. If the path is already valid in the client store, the goal returns immediately and does not read the certificate. That is why the delete above is required before a repeat test.
2. The client asks Harmonia for the narinfo: NAR hash, size, download URL, and references. It does not download the NAR yet.
3. Every reference is substituted first. Each dependency downloads its own NAR and runs the same certificate check.
4. `copyStorePath` downloads this NAR from `http://cache:5000` and stores it. The log line is `copying path '…mario-1.0' from 'http://cache:5000'`. The certificate is not inside the NAR.
5. The client reads `/var/lib/nix-certification/1abb34kvsvwbx70v145jacvdl0qnhps4.json` and checks it. The four steps are the quote (simulated Azure, MRENCLAVE and public-key hash against the manifest), the RSA-PSS signature over `duet_service_result`, the service hashes against the manifest, and the SHA-256 of the NAR just stored. The success line is `JSON certificate verified`.

If a step fails, the path is removed from the client database and `nix-store --realise` exits non-zero. Dependencies that already passed stay. The next realise downloads Mario again and checks the certificate again. The JSON on Harmonia is left in place.

## Stop

```sh
docker-compose down          # keep volumes
docker-compose down -v       # wipe stores and attestations
```

## Volumes

| Volume | Mount | Role |
|---|---|---|
| `harmonia-store` | `/certified_store` | Served Harmonia store |
| `attestations` | `/var/lib/nix-certification` | Verified certificates `<hash>.json` (read by Harmonia) and `pending/` ones waiting for their NAR |
| `publisher-store` | `/nix` | Publisher build store |
| `client-store` | `/nix` | Client store |

`MAX_NAR_BYTES` on cache defaults to 8 GiB (large leaf NARs). Change in `docker-compose.yml` and rebuild/recreate cache if needed.


## Errors and solutions

### Cache image: unable to get local issuer certificate

`nix profile install` inside the cache build fails with `SSL certificate problem: unable to get local issuer certificate` while downloading `channels.nixos.org`. `Docker/cache/ca/` has no corporate certificate. Copy it there and build again:

```sh
cp ../nix-fork/ca/host-ca.crt cache/ca/
docker compose up -d --build
```

### Fork image: git fetch or git apply fails

If `docker build` in `nix-fork/` stops at `git fetch` with `SSL certificate problem: unable to get local issuer certificate`, `nix-fork/ca/` has no corporate certificate. Copy it there (see Setup) and build again.

If the build stops at `git apply` with `patch does not apply`, `NIX_REV` no longer points at `0765ffa540e2adfda7b69efafdbac821ebc48034`. The patches in `nix-fork/nix-src-patches/` only apply to that commit. Drop the `--build-arg NIX_REV=…` override, or regenerate the patches against the new commit.

### CACTEE_HOST_PATH is not set

Compose warns `The "CACTEE_HOST_PATH" variable is not set` and the certifier mount is empty. Create `Docker/.env` with the absolute path of the cactee checkout and the publish token, then run `docker compose up -d --build` again.

### Client: exec node-init permission denied

```text
exec: "/usr/local/bin/node-init": permission denied
```

The client bind-mounts `Docker/node/node-init.sh` over the image entrypoint. The publisher starts because it uses the executable copy inside the image. The host script is not executable. From `Docker/`:

```sh
chmod +x node/node-init.sh
docker compose up -d --force-recreate client
```

### Generator: No zscaler-cert-*.crt

`generer_dockerfiles.py` accepts only a file named `zscaler-cert-*.crt`. `host-ca.crt` in `Docker/node/ca/` is ignored. From `cactee-pkgs/mario-dep`:

```sh
cp ../../Docker/node/ca/host-ca.crt ../../Docker/node/ca/zscaler-cert-host.crt
```

Run the generator again.

### Generator: nix not found

The generator runs `nix` on the host for every package, so Nix must be installed there. `--no-bundle-leaves` is not a way around it: the leaves it writes have no CACTEE input, and the orchestrator refuses them (`expected exactly one CACTEE input (closure export), found 0`). Install Nix on the host and run the generator again.

### Publish: HTTP 401

The orchestrator sends `nixtee-demo-token` unless `Docker/.env` is loaded. The cache expects `HARMONIA_PUBLISH_TOKEN`. From `cactee-pkgs/mario-dep`:

```sh
set -a
source ../../Docker/.env
set +a
```

### Nodes: certifying this would attest a copy

This entry was written for the previous generator, in which a node first looked for its own output on Harmonia (the `harmonia hit` and `harmonia miss` lines below). The current generator does not do that: it fetches only the node's *input* binaries from Harmonia, and the orchestrator skips a package that is already on Harmonia before it calls CACTEE.

```text
harmonia hit: certified binary
no 'building' line in the log: the target was substituted, not built. Certifying this would attest a copy.
```

Port 5000 already has the output, so the node copies it and CACTEE refuses the certificate. A directory named `Docker` makes Compose reuse the volume `docker_harmonia-store`. Stop the orchestrator and replace that volume. This deletes the NARs and certificates in that Harmonia. The fork image stays.

```sh
cd Docker
docker compose down
docker volume ls | grep -E 'harmonia|attestation'
docker volume rm docker_harmonia-store docker_attestations
docker compose up -d
```

Remove the name `volume ls` prints when it differs. The next node run should log `harmonia miss: building from .drv` and `building '...'`.

### Mario: package 225 is skipped

The generator prints:

```text
SKIPPED 225 mario-1.0: recipe /nix/store/9xppv8fi...-mario-1.0.drv is neither in the local store nor on https://cache.nixos.org. Import it on this host (nix-store --import) and regenerate.
```

The recipe of `mario` comes from `mario-src/mario.nix` and is on no public cache. Import it on the host as described in "The root recipe" in `cactee-pkgs/mario-dep/README.md`, run the generator again, then certify only that package, from `cactee-pkgs/mario-dep`:

```sh
set -a
source ../../Docker/.env
set +a
python3 orchestrer_certification.py \
    --cactee-url http://127.0.0.1:8080 \
    --harmonia-to http://127.0.0.1:5000 \
    --from 225 --to 225
```

Success is `published 1abb34kvsvwbx70v145jacvdl0qnhps4-mario-1.0`.

### Client realise prints only the store path

The path is already valid, so Nix does not download it and does not read the certificate. Delete it, then realise it again:

```sh
docker exec nixtee-client bash -lc \
  'unset NIX_REMOTE; nix-store --delete --ignore-liveness /nix/store/1abb34kvsvwbx70v145jacvdl0qnhps4-mario-1.0'

docker exec nixtee-client bash -lc \
  'unset NIX_REMOTE; nix-store --realise /nix/store/1abb34kvsvwbx70v145jacvdl0qnhps4-mario-1.0'
```

The second command logs `copying path ... from 'http://cache:5000'` and ends with `JSON certificate verified`.
