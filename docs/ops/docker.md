# Running MEDRE with Docker

The image contains core, installed adapters and their SDK dependencies. The
persistent `/data` volume holds databases, profiles, keys, media and logs.
The host config file is bind-mounted read-only at `/data/config.yaml`; it is
separate from the named volume. Installing Python packages into this volume
is unsupported: changing packages means pulling or building an image and recreating the container.
Updating bridge configuration needs only a container recreation.

Release publications and manual workflow dispatches publish `ghcr.io/jeremiah-k/medre` tags; pull-request and main pushes only
qualify images and never publish. A tag is available only after successful
publication. If the desired tag does not exist, build locally (below) or
dispatch the workflow for a development tag; see
[Release images and updates](#release-images-and-updates).

## Build and start

Docker Engine with Buildx, Docker Compose v2 or newer, Git, Make and Python 3.11+
are required for local builds. No host Python environment or SDK installation
is needed. Run these commands from the core checkout:

```bash
make build
# Create a safe fake-route sample without overwriting an existing config:
( umask 077; set -C; docker run --rm --user 1000:1000 medre:local config sample > config.yaml )
make check
make run
make logs
```

The sample file is created with owner-only permissions. The default container
UID is 1000, so a host user with UID 1000 can use it directly. If the host owner
has a different UID, grant the container read access with a file ACL, such as
`setfacl -m u:1000:r config.yaml`, before `make check`. With user namespace
remapping, grant access to the mapped host UID instead. Keep the file private;
MEDRE runs as a non-root user.

Replace the sample with your reviewed live configuration before connecting
real transports. Keep secrets in the ignored config or a local Compose override;
never place them in Dockerfile arguments. The default Compose deployment refuses
a missing config file instead of silently creating a directory at its path.
`MEDRE_CONFIG=/absolute/path/config.yaml make run` selects another host config.

The process runs as UID/GID 1000, with a read-only root filesystem, a temporary
`/tmp`, dropped Linux capabilities and a persistent named volume. The image
initializes the volume ownership on first use. Existing volumes and bind mounts
must already be writable by that identity. Use `{state}`, `{data}` and `{cache}`
in config paths; these resolve beneath `MEDRE_HOME=/data`. Keep SQLite databases,
SimpleX and Chatmail profiles, LXMF identities and Reticulum configuration there.
Avoid storing transport state in `/tmp` or anonymous container layers.

`make stop` stops the bridge; `make clean` removes the deployment and network
while retaining its named volume. Before updates, securely back up both the
volume and the host config file selected by `MEDRE_CONFIG` (default
`./config.yaml`), along with any local overrides containing credentials. A
volume-only backup does not include the bind-mounted config. The image
runs `medre run` directly and receives SIGTERM through Compose's init process;
Compose allows 40 seconds for the configured runtime shutdown. Increase this
allowance when your runtime or SDK shutdown budget is longer.

## Prepare a Docker bridge example

The two `examples/configs/docker-*-bridge.yaml` files are SDK-boundary test
templates, not ready-to-run deployment configs. Compose bind-mounts the YAML
unchanged, and MEDRE rejects unresolved `${VAR}` placeholders. A Compose
`env_file` does not resolve them. With the relevant test service already
running, make a new private copy (do not overwrite existing bridge state):

```bash
( umask 077; set -C; cat examples/configs/docker-matrix-bridge.yaml > config.yaml )
# Or use examples/configs/docker-meshtastic-bridge.yaml instead.
```

Edit the private copy with a YAML-aware editor. For the Matrix example, replace
`${MEDRE_HOMESERVER}`, `${MEDRE_USER_ID}`, `${MEDRE_ACCESS_TOKEN}`, and both
occurrences of `${MEDRE_ROOM_ID}` with concrete YAML string values. For the
Meshtastic example, replace `${MESHTASTIC_HOST}` with the daemon's reachable
host name or IP. In the container, `localhost` refers to the container itself,
not the host or another service; use a reachable address on the configured
network. Keep credentials out of command history and version control. Use
proper YAML quoting for values containing special characters. Alternatively,
remove the placeholders and use supported MEDRE environment overrides for
adapter fields, retaining concrete route selectors in YAML.

Run `make check` before `make run` (select another private file via
`MEDRE_CONFIG=/absolute/path/config.yaml` on both commands if needed).
The original integration tests construct their configs programmatically;
these examples do not reproduce their qualification evidence by themselves.

## Adapter and source selection

All seven adapters are included by default, with Matrix E2EE dependencies.
Each adapter remains an independently maintained Python distribution; no
provider-specific Docker release pipeline is required.

```bash
make build ADAPTERS="matrix meshtastic"
make build ADAPTERS=none                  # core-only CLI
make build SOURCE=index                  # packages from PyPI
make build BUILD_OPTIONS="--sources /absolute/path/sources.yaml"
make build ADAPTERS="matrix meshtastic" \
  BUILD_OPTIONS="--local matrix=../medre-adapter-matrix --local meshtastic=../my-radio-fork"
```

The default `SOURCE=git` installs the **exact committed compatibility revisions**
from core's adapter manifest. This is the coordinated, reviewed source set used
for image qualification. It does not follow provider main branches. Core is
built from this checkout, including current edits; the image revision label
marks a dirty checkout. A different core checkout or fork builds its own core.

`SOURCE=index` resolves released adapter packages, including declared SDK pins,
against the core being built. It does not promise the same behavior as the Git
compatibility snapshot. Pin index versions in a source catalog when required.
The effective installed versions and Git source records are retained in
`/opt/medre-build.json`; the immutable image digest identifies the deployed
artifact. Rebuilding a floating index selection may resolve newer packages.
Base images and the uv installer are pinned, while Debian security packages
are resolved at build time.

`--sources` uses the existing [source catalog](adapter-installation.md) grammar;
see `docker/sources.example.yaml`. Omit its core entry, since this image always
builds core from its context. Disabled aliases are excluded from `all`. Local
overrides name selected built-in aliases, include uncommitted edits and install
ordinary wheels, without runtime source mounts or editable imports. Local file
requirements bypass uv's directory wheel cache, including when only Python
source changes. BuildKit receives the explicit local checkout contexts: use
trusted, clean input directories and keep private profiles, keys and credentials
outside them. Do not assume that the core context's allowlist applies to named
contexts. Never point an adapter context at a parent containing other projects.

The source catalog is supplied as a BuildKit secret and not copied into the
image. The root build context allowlist excludes `config.yaml`,
`compose.override.yaml` and `docker/sources.local.yaml`. Selected source URLs
must use public HTTP(S), Git over HTTP(S), or local file URLs, without userinfo,
query strings or unsupported fragments. Archive SHA-256 and package subdirectory
fragments are supported. The builder refuses unsafe direct-source metadata,
including transitive direct sources, before copying the runtime environment;
verification repeats that check in the installed image. Keep credentials out of
URL paths, wheel contents, source trees and build inputs as well. Host Git
credentials are not forwarded. For private provider development, use an explicit
trusted local checkout context. Native SDK assets, including
SimpleX's SQLite library, are resolved and initialized during the build. Image
verification exercises native initialization without contacting chat services.
No operator identities or database profiles are retained in the image.

Use `make verify` to inspect installed providers and perform the offline
SimpleX native-library check, comparing actual packages and direct-source
records with the build report. Official image qualification also checks every
committed provider revision and a terminal runtime/adapter shutdown snapshot.
This is installation and fake-runtime evidence, not proof of service
authentication, encryption interoperability or RF delivery.

## Release images and updates

The Docker Image workflow qualifies the complete image on pull requests and
main. On a published release it saves those tested bytes, then publishes them
as `ghcr.io/jeremiah-k/medre:VERSION` through the existing `release` environment.
Canonical public three-component PEP 440 version tags (optionally prefixed by
`v`) match core's project version exactly; alpha, beta, release candidate and
`.devN` versions must be marked prerelease. A non-prerelease also updates
`latest`. Initial official images target
**linux/amd64**; other architectures require qualification before publication.
A GitHub release predating this workflow has no image automatically created.
The GHCR package is public; anonymous pulls require no authentication.

For a development image, run the Docker Image workflow manually. Select its
workflow branch, optionally set `core_ref` to a different core branch, tag or
full SHA, and choose `dev_tag` (default `dev`; e.g. `dev-my-feature`). The build
resolves that ref once and qualifies the resulting image, then publishes both
`dev_tag` and `dev_tag-FULL_CORE_SHA` through the same release environment.
Manual builds cannot choose `latest`, a release tag or a tag ending in a
full-SHA suffix (reserved for generated provenance tags). Only the workflow
revision derives the allowed publication tags from the event and the resolved
checkout SHA; a selected core checkout cannot grant itself release-tag authority.
PR and main pushes qualify the event SHA; they never publish. The release
environment still authorizes the selected code: qualification is not a security
sandbox for arbitrary untrusted source. Use an image digest for exact lab
qualification; rerunning a build can replace the same SHA-bearing tag with
refreshed system or transitive packages. Adapter refs still come from that core
revision's committed compatibility manifest; no moving provider branches are
implicitly selected.

After an image has been published:

```bash
IMAGE=ghcr.io/jeremiah-k/medre:VERSION make pull
IMAGE=ghcr.io/jeremiah-k/medre:VERSION make check
IMAGE=ghcr.io/jeremiah-k/medre:VERSION make run
```

For custom images, build first and recreate only after the build and config
check succeed. `make build-nocache` bypasses both Docker layer reuse and uv
cache reuse.
Container upgrades use images; `medre sources switch`, `medre adapters install`
and self-upgrade commands manage uv tool environments and are not the image's
package manager. Never copy a host venv into the data volume.

The image defines no generic health probe: offline config validity does not
prove a running Matrix connection, radio link or remote delivery. Docker detects
process exit, Compose restarts the process, and operational qualification uses
the existing diagnostics and transport evidence.

## Hardware and service access

Network transports use normal container networking. For services on the Docker
host, use a reachable host address rather than container-local `localhost`.
Expose no inbound ports unless a configured transport explicitly needs them.
Linux host networking can be selected in a local override when discovery or
Reticulum interfaces require it; review its host-access implications.

For serial radios, copy `docker/serial.example.yaml` to the ignored
`compose.override.yaml`, set `MEDRE_SERIAL_DEVICE` to an owned stable
`/dev/serial/by-id/...` device and set `MEDRE_SERIAL_GID` to its host group ID.
The config uses `/dev/medre-radio`. Add explicit device mappings for additional
radios. No privileged container is required.

Bluetooth uses the host's BlueZ service and existing host pairing. The Linux
D-Bus example provides socket access; the host policy must permit UID 1000.
Docker Desktop does not automatically provide Linux serial devices or host
BlueZ. Prefer TCP gateways or native installation on those platforms.
Physical serial/BLE and live service qualification remain transport-specific;
a passing image smoke test does not replace the hardware lab campaign.
