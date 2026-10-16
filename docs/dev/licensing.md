# Licenses and dependency boundaries

MEDRE-owned core source is distributed under the [MIT License](../../LICENSE).
Each adapter repository licenses its own original source separately; always
read that repository's `LICENSE` and `DEPENDENCY_LICENSES.md`. A Python entry
point, a Git repository, or a virtual environment is **not** a legal boundary
that cancels third-party obligations. This page describes the exact dependency
pins in the October 16, 2026 development snapshot, not a blanket clearance of
all transitive dependencies or a substitute for legal advice.

| Distribution | MEDRE-authored source | Main pinned runtime dependency | Upstream license |
| --- | --- | --- | --- |
| `medre` | MIT | `msgspec`, `pyyaml` | BSD-3-Clause, MIT respectively; check full dependency tree |
| `medre-adapter-matrix` | MIT | `mindroom-nio` | ISC; optional E2EE dependencies are resolved by `[e2e]` |
| `medre-adapter-meshcore` | MIT | `meshcore` | MIT |
| `medre-adapter-meshtastic` | MIT | `mtjk` | GPL-3.0-only |
| `medre-adapter-lxmf` | MIT | `lxmf`, `rns` | Reticulum License (non-OSI; additional use restrictions) |

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
- **Combined bundles:** `mtjk` is GPL-3.0-only, while `rns`/`lxmf` add
  Reticulum License use restrictions that are GPL-incompatible further
  restrictions. A single redistributed artifact containing both dependency
  families cannot rely on either license alone; keep transports separately
  installable (the current per-repository distribution model) and obtain fresh
  legal review before building any all-transports bundle.

## Before publishing the MIT change

1. Confirm copyright ownership or obtain permission for **all** included
   contributions and any imported third-party code. Commit authorship alone
   is not conclusive evidence. Historical authors are predominantly Jeremiah K.
   Bots and the original adapter extraction are also present in the history.
2. Review provenance of any code adapted from MMRelay, Meshtastic, or another
   project. If externally GPL-derived source exists, it cannot be converted to
   MIT without the relevant copyright holders' authorization. Keep original
   notices where required.
3. Review installed artifacts and the **complete resolved dependency tree**;
   include third-party license texts/notices when redistributing binaries.
4. Confirm the scope of LXMF/RNS distribution and acceptable use with qualified
   legal counsel. Do not claim the optional adapter removes upstream terms.
5. Check each repository's `LICENSE`, `pyproject.toml`, README, source headers,
   and built wheel metadata before release. Keep this page synchronized when
   pinned SDKs or the licensing policy change.

This project's change is a grant for MEDRE-authored material only. It is not a
statement that the entire combined runtime has an MIT-only effective license.

## Provenance review record (2026-10-16)

- A token-level similarity scan of all five `src/` trees against MMRelay and
  the mtjk/meshtastic library sources found no third-party-derived expression;
  wire-format field names and documented pattern attributions are not copied
  code.
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
- [LXMF](https://pypi.org/project/lxmf/) and [RNS](https://pypi.org/project/rns/)
- [Official Reticulum License](https://reticulum.network/manual/license.html)
- [Open Source Definition](https://opensource.org/osd)
