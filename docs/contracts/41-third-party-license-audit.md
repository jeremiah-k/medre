# Third-Party License Audit

> Contract version: 2
> Last updated: 2026-05-12
> Track: 2 (Legal and Compliance Readiness)
> Supersedes: Nothing. New document.
> Status: Audit. Records license findings for every runtime dependency, optional extra, and operationally important transitive dependency.

This document catalogs the licenses of every dependency MEDRE relies on at runtime, plus the transitive dependencies that ship alongside them. It separates findings into three tiers: **confirmed** (license text or authoritative metadata read directly), **inferred** (pip metadata or well-known community attribution), and **unresolved** (could not verify from local evidence).

It is a factual record, not legal advice. No license compatibility claims here should be treated as a legal opinion.

## 1. Scope

- All dependencies declared in `pyproject.toml` (core and optional extras).
- Transitive dependencies that ship with those optional extras, up to the point where they are operationally relevant (crypto, serialization, networking).
- Build and dev dependencies noted for completeness, but not analyzed in depth.

## 2. Non-goals

- Providing legal opinions on license compatibility.
- Proposing license changes to any dependency.
- Changing how MEDRE declares or manages dependencies.
- Auditing every transitive dependency to the leaf. The audit stops at packages that are themselves dependencies of dependencies and are not directly imported by MEDRE code.

## 3. MEDRE's own license

MEDRE declares `license = "GPL-3.0-or-later"` in `pyproject.toml` (updated 2026-05-12 from MIT). A top-level `LICENSE` file with the standard FSF GPLv3 text is present. The GPL-3.0-or-later license is compatible with all dependency licenses documented below: BSD-3-Clause (permissive), ISC (permissive), LGPL-3.0-or-later (compatible with GPL-3.0+), and the Reticulum License (restriction clauses reviewed in contract 44).

## 4. Confirmed findings

License text or authoritative project metadata was read directly from local reference repositories or installed package metadata.

### 4.1 msgspec (core, required)

| Field                  | Value                                                                 |
| ---------------------- | --------------------------------------------------------------------- |
| **Distribution**       | `msgspec`                                                             |
| **Version pinned**     | `0.21.1`                                                              |
| **License**            | BSD-3-Clause                                                          |
| **Evidence**           | `pip show msgspec` returns `License-Expression: BSD-3-Clause`         |
| **Compatibility note** | BSD-3-Clause is permissive. No tension with MEDRE's GPL-3.0-or-later. |

msgspec is the sole core dependency. It provides fast serialization and validation. BSD-3-Clause imposes attribution and license text retention, both straightforward to satisfy.

### 4.2 mindroom-nio (optional extra: `matrix`, `matrix-e2e`)

| Field                  | Value                                                                                                                                                                                |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Distribution**       | `mindroom-nio`                                                                                                                                                                       |
| **Import name**        | `nio`                                                                                                                                                                                |
| **Version floor**      | `>= 0.25.3`                                                                                                                                                                          |
| **License**            | ISC (Internet Systems Consortium license)                                                                                                                                            |
| **Evidence**           | `/home/jeremiah/dev/mindroom-nio/LICENSE.md` read directly. Contains full ISC text with copyright notice to Damir Jelić. `pyproject.toml` line: `license = { file = "LICENSE.md" }`. |
| **Compatibility note** | ISC is functionally equivalent to MIT. No tension with MEDRE's GPL-3.0-or-later.                                                                                                     |

mindroom-nio is a fork of matrix-nio. The upstream project is also ISC-licensed. The fork does not appear to have changed the license terms.

### 4.3 vodozemac (optional extra: `matrix-e2e`)

| Field                  | Value                                                                                                                                                                                |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Distribution**       | `vodozemac`                                                                                                                                                                          |
| **Version**            | `>= 0.9` (transitive, from mindroom-nio e2e extra)                                                                                                                                   |
| **License**            | Apache-2.0                                                                                                                                                                           |
| **Evidence**           | `/home/jeremiah/dev/vodozemac-python/LICENSE` read directly. Full Apache-2.0 text with copyright notice to matrix-nio (2024). `pyproject.toml` line: `license = {file = "LICENSE"}`. |
| **Compatibility note** | Apache-2.0 is permissive. Compatible with MEDRE's GPL-3.0-or-later. Requires retaining NOTICE file if one exists.                                                                    |

vodozemac provides the Olm/Megolm cryptographic primitives used for Matrix E2EE. It is a Rust library with Python bindings built via maturin/pyo3.

### 4.4 mtjk (optional extra: `meshtastic`)

| Field                  | Value                                                                                                                                                           |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Distribution**       | `mtjk`                                                                                                                                                          |
| **Import name**        | `meshtastic`                                                                                                                                                    |
| **Version floor**      | `>= 2.7.8`                                                                                                                                                      |
| **License**            | GPL-3.0-only                                                                                                                                                    |
| **Evidence**           | `/home/jeremiah/dev/meshtastic/mtjk/pyproject.toml` line: `license = "GPL-3.0-only"`. `/home/jeremiah/dev/meshtastic/mtjk/LICENSE.md` contains full GPLv3 text. |
| **Compatibility note** | **GPL-3.0-only is copyleft.** This is the most significant license in the dependency tree. See Section 7 for detailed operational implications.                 |

mtjk is a fork of the upstream meshtastic-python project. The upstream project (Apache-2.0) was relicensed to GPL-3.0-only in this fork. MEDRE imports mtjk through adapter isolation with compat guards (`medre.adapters.meshtastic.compat`), meaning the GPL-3.0-only code is only loaded when the `[meshtastic]` extra is installed and the adapter is used.

### 4.5 PyPubSub (optional extra: `meshtastic`)

| Field                  | Value                                                                                                   |
| ---------------------- | ------------------------------------------------------------------------------------------------------- |
| **Distribution**       | `PyPubSub` (also `pypubsub`)                                                                            |
| **Version floor**      | `>= 4.0`                                                                                                |
| **License**            | BSD-2-Clause                                                                                            |
| **Evidence**           | `pip show PyPubSub` returns `License: BSD-2-Clause`. Home-page: `https://github.com/schollii/pypubsub`. |
| **Compatibility note** | BSD-2-Clause is permissive. No tension with MEDRE's GPL-3.0-or-later.                                   |

PyPubSub is included explicitly in the `[meshtastic]` extra because mtjk does not declare it as a dependency, despite requiring it for callback-based packet reception. See `pyproject.toml` lines 35-36 for the rationale.

### 4.6 meshcore (optional extra: `meshcore`)

| Field                  | Value                                                                                                                                                                 |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Distribution**       | `meshcore`                                                                                                                                                            |
| **Version floor**      | `>= 2.3.7`                                                                                                                                                            |
| **License**            | MIT                                                                                                                                                                   |
| **Evidence**           | `/home/jeremiah/dev/meshcore/meshcore_py/LICENSE` read directly. Full MIT text with copyright to Florent de Lamotte (2025). `pyproject.toml` line: `license = "MIT"`. |
| **Compatibility note** | Permissive. No tension with MEDRE's GPL-3.0-or-later.                                                                                                                 |

### 4.7 LXMF (optional extra: `lxmf`)

| Field                  | Value                                                                                                                                                                               |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Distribution**       | `lxmf`                                                                                                                                                                              |
| **Version floor**      | `>= 0.9.6`                                                                                                                                                                          |
| **License**            | Reticulum License                                                                                                                                                                   |
| **Evidence**           | `/home/jeremiah/dev/LXMF/LICENSE` read directly. `setup.py` line: `license="Reticulum License"`. Local version confirmed: `/home/jeremiah/dev/LXMF/LXMF/_version.py` shows `0.9.6`. |
| **Compatibility note** | The Reticulum License is **not an OSI-approved license**. It adds ethical-use restrictions to an otherwise MIT-style grant. See Section 8 for detailed ambiguity analysis.          |

### 4.8 Reticulum / rns (transitive, from `lxmf`)

| Field                  | Value                                                                                                                                                                                        |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Distribution**       | `rns`                                                                                                                                                                                        |
| **Import name**        | `RNS`                                                                                                                                                                                        |
| **Version**            | `>= 1.2.0` (transitive, from LXMF `install_requires`)                                                                                                                                        |
| **License**            | Reticulum License                                                                                                                                                                            |
| **Evidence**           | `/home/jeremiah/dev/Reticulum/LICENSE` read directly. `setup.py` line: `license="Reticulum License"`. Local version confirmed: `/home/jeremiah/dev/Reticulum/RNS/_version.py` shows `1.2.5`. |
| **Compatibility note** | Same Reticulum License as LXMF. Same ethical-use restrictions apply. See Section 8.                                                                                                          |

Reticulum is the networking stack that LXMF builds on. It is a hard transitive dependency: installing `lxmf` pulls `rns` automatically.

## 5. Inferred findings

License identified from installed pip metadata (`License` or `License-Expression` fields) or well-known community attribution. The license text was not read directly from the source repository in these cases.

### 5.1 mindroom-nio transitive dependencies

These packages are installed when `mindroom-nio` is installed. All are runtime dependencies declared in `/home/jeremiah/dev/mindroom-nio/pyproject.toml`.

| Package        | Version installed | License (pip metadata) | Notes                           |
| -------------- | ----------------- | ---------------------- | ------------------------------- |
| aiohttp        | 3.13.5            | Apache-2.0 AND MIT     | HTTP client library             |
| aiofiles       | 24.1.0            | Apache-2.0             | Async file I/O                  |
| h11            | 0.16.0            | MIT                    | HTTP/1.1 protocol library       |
| h2             | 4.3.0             | MIT                    | HTTP/2 protocol library         |
| jsonschema     | 4.26.0            | MIT                    | JSON Schema validation          |
| unpaddedbase64 | 2.1.0             | Apache-2.0             | Base64 encoding without padding |
| pycryptodome   | 3.23.0            | BSD, Public Domain     | Cryptographic primitives        |
| aiohttp-socks  | 0.11.0            | Apache-2.0             | SOCKS proxy support for aiohttp |

### 5.2 mindroom-nio e2e extra transitive dependencies

These packages are installed when the `[matrix-e2e]` extra is used. Declared in `/home/jeremiah/dev/mindroom-nio/pyproject.toml` under `[project.optional-dependencies] e2e`.

| Package      | Version installed | License (pip metadata) | Notes                                                                                                                                     |
| ------------ | ----------------- | ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| atomicwrites | 1.4.1             | MIT                    | Atomic file writes                                                                                                                        |
| cachetools   | 5.5.2             | MIT                    | Caching utilities                                                                                                                         |
| peewee       | 3.19.0            | MIT (inferred)         | pip metadata shows blank for License. Peewee is widely known and distributed as MIT. Source: `https://github.com/coleifer/peewee` README. |

### 5.3 mtjk transitive dependencies

These packages are installed when `mtjk` is installed. Declared in `/home/jeremiah/dev/meshtastic/mtjk/pyproject.toml` under `[tool.poetry.dependencies]`.

| Package           | Version installed | License (pip metadata)      | Notes                                                                                                |
| ----------------- | ----------------- | --------------------------- | ---------------------------------------------------------------------------------------------------- |
| pyserial          | 3.5               | BSD                         | Serial port access                                                                                   |
| protobuf          | 6.33.4            | BSD-3-Clause                | Protocol Buffers                                                                                     |
| tabulate          | 0.9.0             | MIT                         | Table formatting (CLI use)                                                                           |
| requests          | 2.33.1            | Apache-2.0                  | HTTP library                                                                                         |
| PyYAML            | 6.0.3             | MIT                         | YAML parser                                                                                          |
| packaging         | 24.2              | Apache-2.0 / BSD (inferred) | pip metadata shows blank. `packaging` is a PSF project under Apache-2.0 / BSD-2-Clause dual license. |
| typing-extensions | 4.15.0            | PSF-2.0                     | Backported typing constructs                                                                         |
| bleak             | 3.0.1             | MIT                         | Bluetooth Low Energy client                                                                          |

### 5.4 meshcore transitive dependencies

These packages are installed when `meshcore` is installed. Declared in `/home/jeremiah/dev/meshcore/meshcore_py/pyproject.toml` under `dependencies`.

| Package               | Version installed       | License (pip metadata) | Notes                                                                                                                   |
| --------------------- | ----------------------- | ---------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| bleak                 | 3.0.1                   | MIT                    | Also pulled by mtjk. Same package.                                                                                      |
| pyserial-asyncio-fast | (not installed locally) | BSD (inferred)         | Fork of pyserial-asyncio. pyserial is BSD; this fork is widely attributed as BSD. Could not read license text directly. |
| pycayennelpp          | (not installed locally) | Unknown                | CayenneLPP payload encoder. Not in local pip metadata.                                                                  |
| pycryptodome          | 3.23.0                  | BSD, Public Domain     | Also pulled by mindroom-nio. Same package.                                                                              |

### 5.5 Reticulum transitive dependencies

These packages are installed when `rns` is installed. Declared in `/home/jeremiah/dev/Reticulum/setup.py` under `install_requires`.

| Package      | Version installed | License (pip metadata)     | Notes                              |
| ------------ | ----------------- | -------------------------- | ---------------------------------- |
| cryptography | 46.0.7            | Apache-2.0 OR BSD-3-Clause | TLS/crypto backend for Reticulum   |
| pyserial     | 3.5               | BSD                        | Also pulled by mtjk. Same package. |

### 5.6 Build dependencies

| Package    | License (pip metadata) | Notes                                 |
| ---------- | ---------------------- | ------------------------------------- |
| setuptools | MIT                    | Declared in `[build-system] requires` |

### 5.7 Dev dependencies

| Package        | License (pip metadata) | Notes                                             |
| -------------- | ---------------------- | ------------------------------------------------- |
| pytest         | MIT                    | Declared in `[project.optional-dependencies] dev` |
| pytest-asyncio | Apache-2.0             | Declared in `[project.optional-dependencies] dev` |

## 6. Unresolved findings

These packages could not be confirmed from local evidence. The license text was not available in any local reference repository, and pip metadata was unavailable because the packages were not installed in the local environment.

### 6.1 pycayennelpp

| Field                  | Value                                                                                                                        |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| **Distribution**       | `pycayennelpp`                                                                                                               |
| **Role**               | Transitive dependency of `meshcore`                                                                                          |
| **License**            | Unknown                                                                                                                      |
| **Evidence attempted** | Not installed locally. No local reference repository found under `/home/jeremiah/dev/`.                                      |
| **Action needed**      | Check PyPI page (`https://pypi.org/project/pycayennelpp/`) or source repository for license declaration before distribution. |

### 6.2 pyserial-asyncio-fast

| Field                  | Value                                                                                                                                               |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Distribution**       | `pyserial-asyncio-fast`                                                                                                                             |
| **Role**               | Transitive dependency of `meshcore`                                                                                                                 |
| **License**            | BSD (inferred from pyserial lineage, but not confirmed)                                                                                             |
| **Evidence attempted** | Not installed locally. No local reference repository found. The package is a performance-focused fork of `pyserial-asyncio`, which is BSD licensed. |
| **Action needed**      | Verify license from PyPI or source repository before distribution. The fork author may have changed terms.                                          |

## 7. GPL-3.0-only operational implications (mtjk)

mtjk is the only GPL-3.0-only dependency in the tree. This section documents the operational facts relevant to distribution decisions. It does not offer a legal conclusion.

### 7.1 How mtjk enters the dependency tree

mtjk is an **optional extra**, declared in `[project.optional-dependencies] meshtastic`. It is not installed by default. Users who run `pip install medre` get no GPL-licensed code. Users who run `pip install medre[meshtastic]` do.

### 7.2 How mtjk is loaded at runtime

MEDRE's Meshtastic adapter uses a compat guard pattern. The import is behind `medre.adapters.meshtastic.compat.HAS_MESHTASTIC`, which returns `False` when `meshtastic` (the import name of mtjk) is not importable. The GPL-3.0-only code is never loaded unless the user explicitly installs the extra and the adapter is activated.

### 7.3 Distribution scenarios

**MEDRE as a library (import-only use):** When distributed as a PyPI package with optional extras, users who skip the `[meshtastic]` extra never receive GPL-3.0-only code. The standard position of the Free Software Foundation is that optional plugin dependencies behind import guards constitute an "aggregate" rather than a single derived work, though this position has been debated.

**MEDRE bundled with mtjk (e.g., Docker image, fat wheel):** If MEDRE is distributed in a form that includes mtjk alongside it in the same runtime, the GPL-3.0-only may require the entire combined work to be distributed under GPL-3.0-compatible terms. MEDRE's own GPL-3.0-or-later license is compatible with GPL-3.0-only (GPL-3.0-or-later is a superset).

**Practical implication:** Any distribution that bundles MEDRE with the `[meshtastic]` extra should be reviewed by someone with software licensing expertise before publication.

### 7.4 Upstream context

The upstream meshtastic-python project uses Apache-2.0. The mtjk fork specifically chose GPL-3.0-only. This is confirmed in `/home/jeremiah/dev/meshtastic/mtjk/pyproject.toml` line 8: `license = "GPL-3.0-only"`. The `only` suffix means the "or any later version" grant is absent, so GPL-3.0 terms cannot be upgraded to future GPL versions.

## 8. Reticulum License ambiguity (LXMF and RNS)

Both LXMF and Reticulum (rns) use a custom license called the "Reticulum License." It is not an OSI-approved license. This section documents what the license says and where the ambiguity lies.

### 8.1 License text structure

The license was read from two locations:

- `/home/jeremiah/dev/LXMF/LICENSE` (copyright 2020-2025 Mark Qvist)
- `/home/jeremiah/dev/Reticulum/LICENSE` (copyright 2016-2026 Mark Qvist)

The license text is identical in both files. It follows the MIT license format with two additional restriction clauses inserted before the standard warranty disclaimer.

### 8.2 The restrictive clauses

The Reticulum License adds these two conditions to the standard MIT grant:

1. **Harm restriction:** "The Software shall not be used in any kind of system which includes amongst its functions the ability to purposefully do harm to human beings."

2. **AI training restriction:** "The Software shall not be used, directly or indirectly, in the creation of an artificial intelligence, machine learning or language model training dataset, including but not limited to any use that contributes to the training or development of such a model or algorithm."

### 8.3 Why this is ambiguous

**Not OSI-approved:** The Open Source Initiative does not recognize the Reticulum License. It fails the OSI's "no use restrictions" criterion. Software under this license cannot be accurately described as "open source" in the OSI-defined sense.

**Not in standard SPDX catalog:** No standard SPDX identifier exists for this license. pip metadata shows it as the literal string `Reticulum License`. Automated license scanning tools will likely flag it as unrecognized.

**Use restrictions:** Both clauses impose restrictions on _use_, not just distribution or modification. This differs from permissive licenses (MIT, BSD, Apache) and even copyleft licenses (GPL), which generally restrict distribution and modification terms but not the purpose of use. The harm restriction is broadly worded. "Purposefully do harm to human beings" could be interpreted narrowly (weapons systems) or broadly (any system with dual-use potential).

**AI training clause:** This restriction is increasingly common in ethical-use licenses but remains legally untested. It may conflict with fair-use or fair-dealing doctrines in some jurisdictions, or it may be fully enforceable. There is no case law to cite.

**Effect on downstream users:** Anyone distributing MEDRE bundled with the `[lxmf]` extra should be aware that the Reticulum License's use restrictions flow through to end users. MEDRE's own GPL-3.0-or-later license does not override these terms for the LXMF/RNS components.

### 8.4 Operational impact

- MEDRE's use of LXMF and RNS for mesh communication does not appear to trigger the harm or AI-training restrictions under a reasonable reading.
- However, the restrictions exist and are enforceable by the licensor. Organizations with strict license compliance policies may flag this.
- The `pip show lxmf` output confirms: `License: Reticulum License`. Automated compliance scanners will not automatically resolve this to a known category.

## 9. Summary matrix

### 9.1 Direct dependencies

| Package           | Extra      | License                      | Category               | Evidence tier |
| ----------------- | ---------- | ---------------------------- | ---------------------- | ------------- |
| msgspec           | (core)     | BSD-3-Clause                 | Permissive             | Confirmed     |
| mindroom-nio      | matrix     | ISC                          | Permissive             | Confirmed     |
| mindroom-nio[e2e] | matrix-e2e | ISC (+ vodozemac Apache-2.0) | Permissive             | Confirmed     |
| mtjk              | meshtastic | GPL-3.0-only                 | Copyleft               | Confirmed     |
| PyPubSub          | meshtastic | BSD-2-Clause                 | Permissive             | Confirmed     |
| meshcore          | meshcore   | MIT                          | Permissive             | Confirmed     |
| lxmf              | lxmf       | Reticulum License            | Custom, use-restricted | Confirmed     |

### 9.2 Key transitive dependencies

| Package               | Pulled by              | License                    | Category               | Evidence tier |
| --------------------- | ---------------------- | -------------------------- | ---------------------- | ------------- |
| vodozemac             | mindroom-nio[e2e]      | Apache-2.0                 | Permissive             | Confirmed     |
| rns (Reticulum)       | lxmf                   | Reticulum License          | Custom, use-restricted | Confirmed     |
| pycryptodome          | mindroom-nio, meshcore | BSD / Public Domain        | Permissive             | Inferred      |
| cryptography          | rns                    | Apache-2.0 OR BSD-3-Clause | Permissive             | Inferred      |
| aiohttp               | mindroom-nio           | Apache-2.0 AND MIT         | Permissive             | Inferred      |
| protobuf              | mtjk                   | BSD-3-Clause               | Permissive             | Inferred      |
| bleak                 | mtjk, meshcore         | MIT                        | Permissive             | Inferred      |
| pyserial              | mtjk, rns              | BSD                        | Permissive             | Inferred      |
| pycayennelpp          | meshcore               | Unknown                    | Unknown                | Unresolved    |
| pyserial-asyncio-fast | meshcore               | BSD (inferred)             | Unknown                | Unresolved    |

### 9.3 Build and dev dependencies

| Package        | Role          | License    | Evidence tier |
| -------------- | ------------- | ---------- | ------------- |
| setuptools     | Build backend | MIT        | Inferred      |
| pytest         | Dev           | MIT        | Inferred      |
| pytest-asyncio | Dev           | Apache-2.0 | Inferred      |

## 10. Sources consulted

### 10.1 Local reference repositories

| Path                                                     | What it provided                                |
| -------------------------------------------------------- | ----------------------------------------------- |
| `/home/jeremiah/dev/mindroom-nio/LICENSE.md`             | ISC license text (confirmed)                    |
| `/home/jeremiah/dev/mindroom-nio/pyproject.toml`         | License field, dependency list, optional extras |
| `/home/jeremiah/dev/meshtastic/mtjk/pyproject.toml`      | GPL-3.0-only declaration, dependency list       |
| `/home/jeremiah/dev/meshtastic/mtjk/LICENSE.md`          | Full GPLv3 text (confirmed)                     |
| `/home/jeremiah/dev/meshcore/meshcore_py/LICENSE`        | MIT license text (confirmed)                    |
| `/home/jeremiah/dev/meshcore/meshcore_py/pyproject.toml` | License field, version, dependency list         |
| `/home/jeremiah/dev/LXMF/LICENSE`                        | Reticulum License text (confirmed)              |
| `/home/jeremiah/dev/LXMF/setup.py`                       | License declaration, install_requires           |
| `/home/jeremiah/dev/LXMF/LXMF/_version.py`               | Version confirmation (0.9.6)                    |
| `/home/jeremiah/dev/Reticulum/LICENSE`                   | Reticulum License text (confirmed)              |
| `/home/jeremiah/dev/Reticulum/setup.py`                  | License declaration, install_requires           |
| `/home/jeremiah/dev/Reticulum/RNS/_version.py`           | Version confirmation (1.2.5)                    |
| `/home/jeremiah/dev/vodozemac-python/LICENSE`            | Apache-2.0 text (confirmed)                     |
| `/home/jeremiah/dev/vodozemac-python/pyproject.toml`     | License field, build system info                |

### 10.2 Installed package metadata

License fields extracted via `pip show <package>` from the local Python environment at `/home/jeremiah/.platformio/penv/lib/python3.12/site-packages/`.

| Package           | pip metadata field used                           |
| ----------------- | ------------------------------------------------- |
| msgspec           | `License-Expression: BSD-3-Clause`                |
| PyPubSub          | `License: BSD-2-Clause`                           |
| bleak             | `License-Expression: MIT`                         |
| pycryptodome      | `License: BSD, Public Domain`                     |
| pyserial          | `License: BSD`                                    |
| protobuf          | `License: 3-Clause BSD License`                   |
| aiohttp           | `License: Apache-2.0 AND MIT`                     |
| aiofiles          | `License: Apache-2.0`                             |
| h11               | `License: MIT`                                    |
| h2                | `License: The MIT License (MIT)`                  |
| jsonschema        | `License-Expression: MIT`                         |
| unpaddedbase64    | `License: Apache-2.0`                             |
| aiohttp-socks     | `License: Apache-2.0`                             |
| atomicwrites      | `License: MIT`                                    |
| cachetools        | `License: MIT`                                    |
| tabulate          | `License: MIT`                                    |
| requests          | `License: Apache-2.0`                             |
| PyYAML            | `License: MIT`                                    |
| typing-extensions | `License-Expression: PSF-2.0`                     |
| cryptography      | `License-Expression: Apache-2.0 OR BSD-3-Clause`  |
| setuptools        | `License-Expression: MIT`                         |
| pytest            | `License-Expression: MIT`                         |
| pytest-asyncio    | `License-Expression: Apache-2.0`                  |
| packaging         | (blank, inferred Apache-2.0/BSD from PSF project) |
| peewee            | (blank, inferred MIT from upstream README)        |

### 10.3 Project files

| File                                            | What it provided                                                               |
| ----------------------------------------------- | ------------------------------------------------------------------------------ |
| `pyproject.toml` (MEDRE root)                   | All dependency declarations, extras, version pins, MEDRE's own MIT declaration |
| `docs/contracts/34-dependency-reality-audit.md` | Install friction notes, compat guard references, version context               |
