# Licenses and dependency boundaries

MEDRE-owned core source is distributed under the [MIT License](../../LICENSE).
Each adapter repository licenses its own original source separately; always
read that repository's `LICENSE` and `DEPENDENCY_LICENSES.md`. A Python entry
point, a Git repository, or a virtual environment is **not** a legal boundary
that cancels third-party obligations. The table identifies principal runtime
components; exact requirements belong to each adapter's `pyproject.toml` and
its dependency disclosure. It is not an inventory of every transitive library
or a blanket clearance for a combined installation.

| Distribution                | MEDRE-authored source | Main pinned runtime dependency                 | Upstream license                                                                                               |
| --------------------------- | --------------------- | ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `medre`                     | MIT                   | `msgspec`, `pyyaml`, `termaid`                 | BSD-3-Clause, MIT, MIT respectively; check full dependency tree                                                |
| `medre-adapter-matrix`      | MIT                   | `mindroom-nio`                                 | ISC; optional E2EE dependencies are resolved by `[e2e]`                                                        |
| `medre-adapter-meshcore`    | MIT                   | `meshcore`                                     | MIT                                                                                                            |
| `medre-adapter-meshtastic`  | MIT                   | `mtjk`                                         | GPL-3.0-only                                                                                                   |
| `medre-adapter-lxmf`        | MIT                   | `lxmf`, `rns`                                  | Reticulum License (non-OSI; additional use restrictions)                                                       |
| `medre-adapter-simplexchat` | MIT                   | `simplex-chat`                                 | AGPL-3.0-only (official SDK and native library)                                                                |
| `medre-adapter-xmpp`        | MIT                   | `slixmpp`                                      | MIT                                                                                                            |
| `medre-adapter-chatmail`    | MIT                   | `deltachat-rpc-client`, `deltachat-rpc-server` | MPL-2.0; the server wheel bundles a license notice even though its package metadata omits a license expression |

Exact version pins are owned by each repository's `pyproject.toml` and lockfile, not this page.

## Important distinctions

- **Meshtastic:** MIT permits combining MIT-authored code with GPL-covered
  software, but redistributing a combined installation may trigger GPL
  conditions. Do not market an `mtjk`-powered deployment as MIT-only. The
  source grant and downstream redistribution obligations are different.
- **LXMF/Reticulum:** the Reticulum License forbids certain harmful-system
  uses and AI/ML training-data uses. Those conditions remain applicable to
  the upstream packages even though MEDRE-authored adapter source is MIT.
  The Reticulum License does not satisfy the Open Source Definition's
  field-of-endeavor freedom. An in-process plugin and a separate repository
  alone do not establish legal separation; an out-of-process adapter is not an
  automatic legal firewall either.
- **Matrix/MeshCore:** their principal SDKs have permissive licenses, but
  optional crypto libraries and transitively installed components still need
  a dependency inventory and notices review before shipping an installer.
- **Older copies:** previously distributed GPL-licensed revisions retain the
  rights granted to their recipients. Changing the license on a new revision
  does not revoke those grants.
- **SimpleX:** the Python client declares AGPL-3.0-only. Its native library
  also carries upstream terms. Preserve source availability, notices, and any
  applicable network-use obligations when assessing the actual installation;
  the adapter's MIT grant does not relicense either component.
- **Chatmail:** the RPC client's metadata declares MPL-2.0. The server wheel's
  packaged `deltachat_rpc_server/LICENSE` supplies an MPL-2.0 notice although
  its metadata omits a license expression. Bundled native dependencies require
  their own inventory. MPL file-level copyleft is not a claim that every
  separate MEDRE-authored file must be relicensed.
- **Combined distribution:** GPL-covered combined works and collections of
  separate programs are different cases. Reticulum's additional restrictions
  need particular attention if a distribution forms a combined work with
  GPL-covered components. A shared installer, process boundary, or repository
  count alone does not settle that question. Review the actual shipped
  artifact and its source/notices rather than treating every multi-adapter
  installation as either automatically cleared or automatically forbidden.

## Project source and redistribution

The source-license decision is to grant MIT rights for MEDRE-authored material,
with attribution and provenance preserved. It is not a plan to relicense SDKs
or to redesign transport ownership merely to claim a licensing exemption.
Core routing and adapter/native ownership boundaries remain technical contracts.

For redistribution, identify the selected core and adapter revisions, inspect
resolved runtime components and downloaded native artifacts, and preserve the
notices and source availability required by those components. The per-adapter
disclosures are a starting inventory, not the full terms of a bundled binary.
Check the relevant license texts for the intended packaging and operation;
legal conclusions about a combined work depend on that concrete arrangement.

The existing MIT source grant does not certify rights in unrelated imported
code. Retain the provenance record and any necessary copyright permissions
when adapting source, including code originally contributed to another project.

## Provenance review record (2026-10-16)

- The October 16 review recorded a token-level similarity scan of the five
  source trees then examined against MMRelay and mtjk/meshtastic. It reported
  no identified third-party-derived expression beyond protocol terminology
  and documented patterns. That dated observation is not proof of ownership
  and does not cover every later adapter or source revision.
- `medre-adapter-matrix` module `src/medre/adapters/matrix/identity.py` is
  adapted from `matrix/e2ee_identity.py`, which was contributed by this
  project's author to his own MMRelay fork; the copyright in those lines is
  retained by Jeremiah K. (MMRelay has no contributor license agreement or
  assignment), so the adaptation carries no third-party grant dependency.
- `docs/dev/testing.md` reuses a trivial async-fake helper originally authored
  by the same contributor in MMRelay's testing guide.
- `medre-adapter-meshcore` `session.py` documents a BLE-disconnect pattern
  adapted at the idea level from MMRelay with rewritten implementation.
- Commit authorship across all five repositories is overwhelmingly Jeremiah K;
  the remaining authors are automation (renovate, an applied review suggestion,
  and the adapter-extraction tooling) acting under the project owner's
  direction.
- Remaining human confirmation: that no employment or assignment agreement
  covers these contributions.

## Upstream authorities

- [BSD-3-Clause msgspec](https://pypi.org/project/msgspec/)
- [MIT PyYAML](https://pypi.org/project/PyYAML/)
- [BSD-2-Clause PyPubSub](https://pypi.org/project/Pypubsub/)
- [ISC-licensed mindroom-nio](https://pypi.org/project/mindroom-nio/)
- [MIT-licensed meshcore](https://pypi.org/project/meshcore/)
- [GPL-3.0-only mtjk](https://pypi.org/project/mtjk/)
- [AGPL-3.0-only simplex-chat](https://pypi.org/project/simplex-chat/)
- [MIT-licensed slixmpp](https://pypi.org/project/slixmpp/)
- [MPL-2.0 deltachat-rpc-client](https://pypi.org/project/deltachat-rpc-client/) and [deltachat-rpc-server](https://pypi.org/project/deltachat-rpc-server/)
- [LXMF](https://pypi.org/project/lxmf/) and [RNS](https://pypi.org/project/rns/)
- [MPL file-level obligations](https://www.mozilla.org/en-US/MPL/2.0/FAQ/)
- [SimpleX native source license](https://github.com/simplex-chat/simplex-chat/blob/v7.0.2/LICENSE)
- [Official Reticulum License](https://reticulum.network/manual/license.html)
- [Open Source Definition](https://opensource.org/osd)
