# Secure Credential and Identity Handling

> Last updated: 2026-05-10
> Scope: Guidance for handling secrets across all MEDRE transports
> Status: Guidance document. No code changes required.

This runbook provides guidance for handling secret material (access tokens, private keys, identity files) when operating MEDRE adapters against real services and hardware.


## 1. Principles

1. **Environment variables for secrets.** All secret material must be provided via environment variables, not command-line arguments, config files checked into version control, or hardcoded strings.
2. **Never commit credentials.** Files containing tokens, private keys, or identity data must be excluded from git. Use `.gitignore` patterns.
3. **Store files outside the repo tree.** If a secret must be stored as a file (e.g., LXMF identity key), store it outside the repository directory or in a path explicitly excluded by `.gitignore`.
4. **Never log tokens or private keys.** Adapters and tests must not log secret material. Diagnostic output and error messages must exclude raw credentials.


## 2. Per-Transport Guidance

### 2.1 Matrix

| Secret | Env var | Handling |
|--------|---------|----------|
| Access token | `MATRIX_ACCESS_TOKEN` | Read from env var only. Never logged. Never committed. |
| Device ID | `MATRIX_DEVICE_ID` | Not secret, but should be stable for E2EE crypto store continuity. |
| Store path | `MATRIX_STORE_PATH` | Not secret, but the crypto store directory contains sensitive key material. Exclude from git. |

**Built-in protection:** `MatrixConfig.__repr__()` redacts the access token to a 3-character preview (`syt_…`) to prevent accidental credential leakage in logs and debug output. No code change is needed — this is already implemented.

**Token rotation:** If the access token is compromised or revoked, generate a new one from the Matrix client (e.g., Element → Settings → Help & About → Access Token) and update the environment variable. Restart the adapter to pick up the new token.

### 2.2 Meshtastic / MeshCore

No secrets are required. Connection parameters (`MESHTASTIC_HOST`, `MESHCORE_HOST`) are network addresses, not credentials. Radio channel configuration is not secret at the MEDRE layer (channel pre-shared keys are managed at the firmware level).

### 2.3 LXMF

| Secret | Env var | Handling |
|--------|---------|----------|
| Identity file | `LXMF_IDENTITY_PATH` | Points to a 64-byte private key file. Must have restrictive file permissions (`chmod 600`). Never committed to git. Never logged. |

**Identity file protection:**

```bash
# Create the identity file with restrictive permissions
chmod 600 /path/to/identity.key

# Verify permissions
ls -la /path/to/identity.key
# Expected: -rw------- (600)
```

**Never copy identity files between instances.** Each LXMF identity is unique. Sharing or duplicating identity files compromises the Reticulum routing and identity system.


## 3. Git Exclusion

Ensure the following patterns are in `.gitignore`:

```
# Credential files
*.key
*.pem
*.token
identity*
nio-store/
crypto-store/

# Environment files (may contain secrets)
.env
.env.*
```

MEDRE's existing `.gitignore` already excludes common patterns. Verify before adding identity or token files.


## 4. Testing

Live test harnesses (`test_*_live.py`) read secrets exclusively from environment variables. Tests never log token values or identity file contents. The `@require_live` decorator skips tests when required env vars are absent, preventing accidental credential prompts during normal test runs.

**Running live tests:**

```bash
# Set secrets in the calling shell (never in a script that gets committed)
export MATRIX_ACCESS_TOKEN="syt_..."
export LXMF_IDENTITY_PATH="/secure/path/identity.key"

# Run live tests
pytest tests/test_matrix_live.py -m live -v
```


## 5. Docker and Deployment

When deploying MEDRE in containers or orchestrated environments:

- Inject secrets via environment variables, not build args or baked-in config files.
- Use Docker secrets, Kubernetes secrets, or equivalent orchestrator secret management.
- Never include secret material in container images or build layers.
- Mount LXMF identity files as read-only volumes from a secrets manager.
