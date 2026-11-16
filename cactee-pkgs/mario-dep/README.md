<!--
© 2026 Nokia
Licensed under the MIT License
SPDX-License-Identifier: MIT
-->

# mario-dep

The full build closure of `mario-1.0`, turned into one CACTEE package per
derivation, ordered from the leaves up to the root.

`mario` is a trivial C program used as a demo. It has 4 runtime references and
3 direct build inputs, but building it pulls in 225 derivations. Certifying a
package is only worth something if its dependencies are certified too, so the
whole closure has to be walked and certified bottom-up. That is what this
folder holds.

## The loop

The packages are not independent. They form a chain that has to be walked in
order, and each turn feeds the next. The generator emits **two kinds** of
package. Each has exactly **one CACTEE input**; CACTEE hashes it and lists its
file name in the certificate, so the certificate says what went in.

**Leaves** (no `.drv` input: tarballs, patches, `busybox`, …):

1. Generator exports the leaf store closure into
   `inputs/<hash>-<name>.export` (host Nix + `--drv-source`), declared as the
   CACTEE input
2. CACTEE copies it to `/tmp/inputs` and runs the container with the network
   off (`run-leaf.sh`): import, **check the output against the hash pinned in
   the `.drv`**, dump the NAR
3. Orchestrator publishes the certificate, then the NAR, to Harmonia

**Every other node:**

1. Generator exports the node's **recipe** (the `.drv` closure: this `.drv`, its
   input `.drv`s and the sources they declare, no binaries) into
   `inputs/<hash>-<name>.drv.export`. That is the CACTEE input, so the
   certificate records **which recipe was executed**. It also lists the output
   paths of the node's direct inputs in `code/input-paths.txt`
2. `docker build` (network on) copies those input binaries from Harmonia, which
   is the only substituter
3. CACTEE runs the container with the network off (`run-node.sh`): import the
   recipe, **build it with no substituter**, dump the outputs and the sources
   the recipe declares
4. Orchestrator publishes the certificate, then the NAR, of each output, for the
   next nodes

The cache refuses a NAR that does not come with a certificate that verifies
(`Docker/README.md`, "The import API"). Verifying certificates when a client
fetches a package, or when a node copies its inputs, is the job of a cert-aware
Nix client (Muhammad's Nix fork), not of this folder.

By the time `mario` itself is built, every input in its closure should already
sit on Harmonia. `00-build-order.txt` marks each line `leaf` or `node`.

## Contents

| Path | Role |
|---|---|
| `mario-drv-graph.dot` | raw dependency graph, extracted from the store |
| `mario-drv-only.dot` | same graph reduced to derivations, layered |
| `mario-drv-dupliques.html` | interactive layered view, open in a browser |
| `generer_dockerfiles.py` | generates the package tree from a `.dot` |
| `orchestrer_certification.py` | bottom-up certify via CACTEE HTTP, publish to Harmonia (no input chaining) |
| `publish_nar.py` | PUT one certificate, then its NAR, to the cache import API on port 5001 |
| `generated/` | 225 CACTEE packages, one per derivation |
| `generated/00-build-order.txt` | the order to build them in |

Graphviz renders the raw `.dot` at about 32000 x 3500 points, which is
unreadable. Use the HTML view instead.

## Numbers

| Metric | Value |
|---|---:|
| Nodes in the build closure | 329 |
| Derivations | 225 |
| Sources, patches, scripts | 104 |
| Real builds | 137 |
| Fixed-output fetches | 88 |
| Runtime closure of `mario` | 8 |
| Direct runtime references | 4 |
| Build depth | 48 layers |
| Derivations taking `bootstrap-tools` as a direct input | 135 |

Of the 88 fixed-output derivations, 86 are source tarballs, patches or shell
scripts. **Two are binaries**: `bootstrap-tools.tar.xz` and `busybox`. They sit
at the bottom of the graph, nothing builds them, and 135 derivations depend
directly on the first. Everything else in the closure can be audited by
reading it.

The 225 derivations carry only 170 distinct names. 36 names exist several times
with different hashes: `gnu-config` six times, `bash-5.2p37` five. nixpkgs
rebuilds them once per bootstrap stage, so they are distinct derivations with
distinct store paths, and each needs its own certificate. Store paths are
unique throughout, so no derivation is generated twice.

## Extracting the graph

Run where the store lives, inside the publisher container:

```sh
docker exec nixtee-publisher sh -c \
  'nix-store -q --graph $(nix-store -qd /nix/store/1abb34kvsvwbx70v145jacvdl0qnhps4-mario-1.0)' \
  > mario-drv-graph.dot
```

An edge `"A" -> "B"` means **A is an input of B**. The out-degree of a node is
the number of derivations that directly depend on it.

Counts quoted above:

```sh
nix-store -qR $(nix-store -qd <out>) | wc -l              # 329
nix-store -qR $(nix-store -qd <out>) | grep -c '\.drv$'   # 225
nix-store -qR <out> | wc -l                               # 8
nix-store -q --references <out>                           # 4
```

## Generating the packages

The Harmonia URL written into the Dockerfiles must be reachable **from
`docker build`**, not from the compose network. `test_docker.py` asks the
host's Docker daemon to build the image on Docker's default bridge network,
where the compose service name `cache` does not resolve. Use the bridge
gateway instead: the `cache` container publishes port 5000 on the host.

```sh
GW=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')   # usually 172.17.0.1

# check that a container on the default network reaches Harmonia
docker run --rm python:3.12-slim-bookworm python3 -c \
  "import urllib.request;print(urllib.request.urlopen('http://$GW:5000/nix-cache-info').read().decode())"

python3 generer_dockerfiles.py mario-drv-graph.dot \
    -o generated \
    --ca ../../Docker/node/ca \
    --drv-source https://cache.nixos.org \
    --harmonia "http://$GW:5000" \
    --substituter "http://$GW:5000" \
    --no-require-sigs
```

`--substituter` holds **only Harmonia**. Do not add `https://cache.nixos.org`
to it: see "The silent fallback" below. The orchestrator is different: it runs
inside the publisher container, on the compose network, and keeps publishing
to `http://cache:5000`.

| Option | Role |
|---|---|
| `-o`, `--outdir` | where the packages are written (required) |
| `--ca` | folder with `zscaler-cert-*.crt` (required) |
| `--drv-source` | where the host (Nix) prefetches leaf closures and node recipes before bundling them into `inputs/` |
| `--harmonia` | where a node's **input** binaries are copied from at `docker build` (`HARMONIA_URL` build argument; default: first URL in `--substituter`) |
| `--substituter` | `substituters` in the node image's `nix.conf` (`SUBSTITUTERS` build argument); Harmonia only |
| `--trusted-key` | `trusted-public-keys` entry for the substituter |
| `--no-require-sigs` | for an unsigned cache, such as a local Harmonia |
| `--root` | pick the root explicitly; default is the node with no dependents |
| `--limit N` | generate only the first N packages, bottom-up |

`--ca` is required because `test_docker.py` tars the whole `code/` folder and
the Dockerfile does `COPY ca/`. Behind the Nokia proxy the certificates have to
be there for the container to reach `cache.nixos.org`. `Docker/node/ca/` holds
the real Zscaler certificates; a folder holding only a placeholder `.crt` makes
every recipe fetch fail on TLS. Keep them out of git: `**/ca/` in `.gitignore`.
Without a proxy an empty `zscaler-cert-none.crt` is enough.

### The root recipe

`mario` is a local expression (`mario-src/mario.nix`), so its recipe is on no
public cache and the generator cannot fetch it. It skips package 225 and says so:

```
SKIPPED 225 mario-1.0: recipe /nix/store/9xppv8fi...-mario-1.0.drv is neither in the local store nor on https://cache.nixos.org. Import it on this host (nix-store --import) and regenerate.
```

The other 224 packages are not affected. To get the 225th, instantiate `mario`
in the publisher, export its recipe closure, import it on the host and run the
generator again:

```sh
DRV=$(docker exec nixtee-publisher sh -c 'nix-instantiate /mario-dep/mario-src/mario.nix')
echo "$DRV"    # must be /nix/store/9xppv8fijaxf857a49a3xknjs06lml9a-mario-1.0.drv
docker exec nixtee-publisher sh -c "nix-store --export \$(nix-store -qR $DRV)" > mario-recipe.export
nix-store --import < mario-recipe.export
```

If `$DRV` is a different path, the publisher's nixpkgs is not the one the graph
was extracted from: extract the graph again ("Extracting the graph") and
regenerate everything.

### Three roles, on purpose

| Flag | What it carries |
|---|---|
| `--drv-source` | host prefetch of leaf closures and node recipes, which end up in `inputs/` |
| `--harmonia` | the node's **input** binaries, copied at `docker build` |
| `--substituter` | `substituters` in the image's `nix.conf`: Harmonia only |

A **recipe** is fetched by exact store path, and Nix rejects any content whose
hash does not match that path. A tampered `.drv` cannot be served under the
path you asked for, so pulling recipes from `cache.nixos.org` is
self-verifying. Checked on 2026-09-29: `cache.nixos.org` does serve the node
recipes (`nix path-info --store https://cache.nixos.org <drv>` succeeds), and
a `nix copy` of a `.drv` brings its recipe closure along: input `.drv` files
and source scripts, no binaries.

A **binary** is not. That is the whole reason this project exists. After the
leaves, the input binaries of a node come from Harmonia (copied at
`docker build`), and the node itself is built from its recipe at run.

### The silent fallback

If Harmonia is unreachable or misses an input, and `cache.nixos.org` is also in
`--substituter`, Nix does **not** fail: it logs a warning and takes the input
from upstream. The node is then built, certified and published as if the chain
had been used.

This happened on 2026-09-29 for node 003 (`bootstrap-tools`), generated with
`http://cache:5000` and `cache.nixos.org` as substituters:

```
error: unable to download 'http://cache:5000/nix-cache-info': Could not resolve host: cache
harmonia miss: building from .drv
copying path '/nix/store/2pizl7lq...-bootstrap-tools.tar.xz' from 'https://cache.nixos.org'...
copying path '/nix/store/p9wzypb8...-busybox' from 'https://cache.nixos.org'...
building '/nix/store/05q48dcd...-bootstrap-tools.drv'...
```

Both inputs were already certified on Harmonia; neither came from it. The
orchestrator still reported `done 1 failed 0`.

With the gateway address and Harmonia as the only substituter, the same node
logs:

```
harmonia miss: building from .drv
copying path '/nix/store/2pizl7lq...-bootstrap-tools.tar.xz' from 'http://172.17.0.1:5000'...
copying path '/nix/store/p9wzypb8...-busybox' from 'http://172.17.0.1:5000'...
building '/nix/store/05q48dcd...-bootstrap-tools.drv'...
```

and published the same output path (`razasrvd...-bootstrap-tools`), as it
should: the inputs are fixed-output, so their bytes are identical whichever
cache served them. The build is bottom-up, so every input of a node is
certified before the node runs; a missing input now **fails the node** instead
of being fetched elsewhere. Only the recipes still come from `cache.nixos.org`.

The run scripts write `/tmp/inputs-used` and copy it to
`/tmp/outputs/inputs-used.txt`:

- leaf: `leaf bundled, verified <mode> <algo>:<hash>`, then the files received in
  `/tmp/inputs` — the output was checked against its `.drv`
- node: `built offline from recipe <hash>-<name>.drv.export`, then the input
  binaries copied from Harmonia at build (`inputs (from Harmonia at build):`)
  and the sources the recipe declares (`sources:`)

`inputs-used.txt` lists **which** inputs were used, not **which cache** served
them. For that, read the `copying path ... from '...'` lines of the build log.

## Build order

`generated/00-build-order.txt` lists the 225 packages grouped by layer, from
the bottom. Layer numbering matches the HTML view, so `L48` is the deepest and
holds the two binary roots.

```
# ===== HTML L48 (from the bottom) =====
001  L48  leaf  /nix/store/xjkydxc0...-bootstrap-tools.tar.xz.drv
002  L48  leaf  /nix/store/0m4y3j4p...-busybox.drv
003  L47  node  /nix/store/05q48dcd...-bootstrap-tools.drv
```

A derivation can only be built once all of its inputs exist, so this order is
the topological depth, not the distance from `mario`. Following it is what
makes the loop above work.

The chain is narrow and deep. Most layers hold one or two packages, so there is
little to parallelise.

## Running one package

```sh
cd <cactee>/examples/dev_test_docker
python3 test_docker.py <path>/generated/001-bootstrap-tools.tar.xz-xjkydxc0/config.json
```

The harness (`prepare_inputs`) copies every file of a `"upload_method":
"direct"` input folder into `test_temp/tmp/inputs`, tars `code/`, builds the
image with network access, then runs the container with networking disabled,
`/tmp/inputs` and `/tmp/outputs` mounted, and collects `/tmp/outputs`. The
real service (`property_computation_request.py`) does the same: image built
from the code, inputs mounted at run, `network_disabled=True`.

`prepare_folders` never clears `test_temp/tmp/inputs` ("use inputs as if it is
like a cache"), so when you run the harness by hand a package's `/tmp/inputs`
still holds the exports of the packages run before it. The simulated service
(`Docker/cactee/cactee-server.py`) empties that folder before every package, as
the real service starts each request from a fresh one. The run scripts also read
only their own export, by name (`$LEAF_EXPORT`, `$NODE_EXPORT`), so leftovers
would be ignored in any case.

Watch the build log rather than the exit status. `test_docker.py` does not stop
when the image build fails: it will run a stale image from an earlier build and
report success.

## What the generated Dockerfile does

Leaves (`is_leaf`: no `.drv` input) ship their store closure as one
`inputs/<hash>-<name>.export` (`nix-store --export`: each path's NAR plus its
path, references and deriver). `config.json` declares `inputs/` as a CACTEE
input. Nothing leaf-specific happens at `docker build`; the image does not
contain the closure. At container **run** (network off), `run-leaf.sh`:

1. fails if `/tmp/inputs/$LEAF_EXPORT` is missing
2. imports it and checks that `$DRV` arrived
3. reads the fixed-output hash from the `.drv` itself, in its ATerm form
   (`Derive([("out","<path>","sha256","<hex>")],...`; `r:sha256` means a
   recursive/NAR hash), not from `nix derivation show`, whose JSON changes
   across Nix versions
4. refuses anything that is not a single-output fixed-output derivation, a
   `text` hash, or an algorithm other than sha1/sha256/sha512, and checks that
   `LEAF_OUTPUTS` is exactly the output the `.drv` declares
5. recomputes the hash (`sha256sum` of the file for `flat`, of
   `nix-store --dump` for `nar`) and fails on mismatch
6. dumps the output NAR to `/tmp/outputs`

`nix-store --import` alone does not do step 5: it only checks each NAR against
the metadata the host wrote next to it. Without the check, the certificate
would attest an unchecked copy of whatever the host exported.

The generator prefetches missing paths from `--drv-source` using host Nix, so Nix
is required on the host. `--no-bundle-leaves` still writes the old online leaf
Dockerfiles, but they carry no CACTEE input and the orchestrator refuses them, so
do not use it for a certification run.

Every other node ships its **recipe**: `inputs/<hash>-<name>.drv.export` is the
`nix-store --export` of `nix-store -qR <drv>` (the `.drv`, its input `.drv`s and
the sources they declare, no binary). CACTEE hashes the file and lists it by
name in the certificate, so the certificate says which recipe was executed.
The input binaries are **not** an attested input: `code/input-paths.txt` lists
the outputs of the node's direct input derivations, and the Dockerfile copies
them from Harmonia while the image is built (network on):

```dockerfile
ENV DRV=/nix/store/<hash>-<name>.drv
ENV NODE_EXPORT="<hash>-<name>.drv.export"
ARG HARMONIA_URL                       # from config.json "code_build_args"
COPY input-paths.txt /tmp/input-paths.txt
RUN if [ -s /tmp/input-paths.txt ]; then \
      nix copy --from "$HARMONIA_URL" $(cat /tmp/input-paths.txt); \
    fi
COPY run-node.sh /usr/local/bin/run-node.sh
CMD ["sh", "/usr/local/bin/run-node.sh"]
```

(abridged: `--no-check-sigs` is added when the generator ran with
`--no-require-sigs`.) At container **run** (network off), `run-node.sh`:

1. fails if `/tmp/inputs/$NODE_EXPORT` is missing
2. imports the recipe and checks that `$DRV` arrived
3. lists the outputs of the recipe (`nix-store -q --outputs`) and the sources it
   declares
4. builds it with `nix-store --realise --option substituters "" "$DRV"`
5. dumps the outputs **and the declared sources** to `/tmp/outputs/*.nar`:
   outputs reference them at run time (stdenv embeds `move-docs.sh`)

**The `--realise` with `substituters ""` is the trust boundary.** Nix has no
cache to copy the **target** from and has to execute the build. The log shows

```
this derivation will be built:
building '/nix/store/<hash>-<name>.drv'...
```

The orchestrator posts nodes with `X-Require-Build: 1`, and the CACTEE container
(`Docker/cactee/cactee-server.py`) refuses a node whose log shows a substitution
and no `building '...'` line: certifying that would attest a copy. A node that
Harmonia already has is skipped by the orchestrator before anything is sent to
CACTEE.

Output paths are resolved from the recipe with `nix-store -q --outputs`, so
nothing has to be known in advance and derivations with several outputs are
handled. `libunistring` has four.

## Trust base

Dropping the input `--realise` makes the container rebuild the whole bootstrap
ladder from source instead of pulling it.

| Variant | Trust base | Time | Valid Nix path |
|---|---|---|---|
| Inputs pulled (current) | 25 paths | ~1 min | yes |
| Inputs rebuilt in one container | 2 fixed-output binaries | hours | yes |
| Upstream sources on a Debian toolchain | Debian's gcc | minutes | no |
| Full-source bootstrap (Guix `hex0`) | a few hundred bytes | days | no |

The 25 was measured for `libunistring`; it is larger for `gcc` or `glibc`.

Ekin ruled out the second row on 2026-09-08: rebuilding a package's whole
closure inside one container gives none of those dependencies a certificate of
its own, so the next package that needs any one of them has to redo the work.
Certificates are only reusable if each package is built and certified
separately. That is what the loop at the top of this file does.

Neither of the first two removes the binary root either. `bootstrap-tools.tar.xz`
is a fixed-output derivation, so `substituters ""` does not stop it being
downloaded. The second variant shrinks the trust base from 25 paths to 2
binaries; it does not empty it.

The last two rows are not implemented here. Building from upstream sources on a
pinned `debian:12-slim` toolchain works and takes minutes, but the output is a
plain tree rather than a store path, so nothing in Nix will consume it. The
Guix full-source bootstrap starts from a few hundred bytes of auditable hex and
is the only approach that removes the prebuilt compiler; it costs days of
compute and produces no Nix store path either.

## Verified

`libunistring` was built this way and the result matches the store byte for
byte:

```
$ sha256sum test_temp/tmp/outputs/libunistring.nar
6f6a1bcbdc9e8acbd2349a70f36a7125195bf60273a0fb66ee6638ba2c3bfea4

$ docker exec nixtee-publisher sh -c \
    'nix-store --dump /nix/store/7r0k7ywz...-libunistring-1.3 | sha256sum'
6f6a1bcbdc9e8acbd2349a70f36a7125195bf60273a0fb66ee6638ba2c3bfea4
```

Two consequences. The build is reproducible: a container on WSL, starting from
the derivation alone, produced the same bytes as the path Hydra built. And the
`NarHash` in a narinfo is the sha256 of the uncompressed NAR, so what CACTEE
would hash is exactly what Harmonia serves. The compose Harmonia serves NARs
uncompressed (`Compression: none`); a cache that serves `.nar.xz` has to be
decompressed before comparing.

All 88 leaves were certified on 2026-09-29 with the in-TEE hash check: 88
`leaf check OK`, 88 paths in the certified store, both hash modes exercised
(`bootstrap-tools.tar.xz` flat, `busybox` nar):

```
leaf check OK: /nix/store/2pizl7lq...-bootstrap-tools.tar.xz flat sha256:61096bd3...
leaf check OK: /nix/store/p9wzypb8...-busybox nar sha256:42b4c49d...
```

The cache registers each NAR under its original store path, with references
computed from the NAR (`Docker/cache/nar-import-server.py`). Checked with
`bash-interactive`: the references found match `nix-store -q --references`,
and a client pulled its 7-path closure from Harmonia into an empty store,
`nix-store --verify-path` OK.

## Open questions

- Whether Harmonia should serve the `.drv` files too. `cache.nixos.org`
  serves the node recipes today, and recipes are self-verifying, so this is
  no longer blocking.
- Whether fixed-output derivations should be certified at all. They download
  and check a hash; they build nothing. With the in-TEE check, a leaf
  certificate attests "this content matches the hash pinned by the recipe",
  not a build. If the answer is no, the scope drops from 225 packages to 137.
- The certification service is not available yet. `Docker/cactee/` runs
  `test_docker.py` and returns a **simulated** certificate in the Nokia format
  (signed with a throw-away key): no TEE, and the quote is crafted.
- Leaves and nodes both do their real work at run, with the network off. The
  only networked step of a node is `docker build`, where it copies its input
  binaries from Harmonia. Those binaries are **not** attested inputs: the
  certificate names the recipe (and the input paths in the code), not the
  bytes of the inputs.
- Verifying the certificates of those inputs when they are copied needs the
  cert-aware Nix client (Muhammad's Nix fork). It is not in this folder.
- The cache import API refuses a NAR that does not come with a certificate that
  verifies (quote, signature, expected service, NAR hash). With the simulated
  CACTEE the Azure token check is skipped (`CACTEE_AZURE_SIMULATE=1`).
