# Provider diagnostics and test ownership

- Share route-aware canonical smoke ingress across normal smoke, run sessions,
  and drills. Run-session callback mode uses `AdapterContract.publish_inbound`,
  independent of provider-specific native packet simulation signatures. Always
  stop the runtime if injection setup fails or is cancelled.
- Select only declared fake providers for synthetic lifecycle drills. Reject
  unexpected construction faults before injecting startup faults, and verify
  construction-failure evidence came from the injected exception.
- Quarantine broken and conflicting entry-point providers while retaining
  diagnostics. Preserve help and healthy providers; report `broken` separately
  from `missing` in installation preflight. Configured broken providers fail
  with actionable configuration errors, identifying the actual provider distribution.
- Generate samples from adapter-owned SDK-free `AdapterSample` contributions.
  Include only installed providers with fake support; create two instances for
  one contribution, or a loadable empty configuration with installation guidance
  when none contribute. Native values and addresses belong to the adapter; core assigns unique IDs.
- Match attribution IDs by exact or bounded longest transport names, with native
  evidence and explicit hints taking precedence. Avoid provider-prefix collisions.
- Base recovery scan duplicate-send advice on unresolved delivery evidence,
  including independent community platforms; omit warnings for an empty scan.
- Replace the adapter-free CI file allowlist with explicit module/suite ownership
  and an import dependency guard. Separate native CLI configuration fixtures from
  generic CLI helpers. The complete compatibility suite remains unchanged.
- Use manifest distribution identities and SDK extras across core contracts,
  Docker integration, and standalone adapter SDK CI. Pin Python before resolving
  compatibility inputs and record standalone core/adapter revision pairs.
- Include installed namespace and community-package source in opt-in architecture
  audits; classify shared fake infrastructure without phantom transports. Clarify
  wheel/source inventory mismatch diagnostics and stale-cache cleanup guidance.

- Derive dependency logger defaults from installed SDK declarations and adapter-owned logger policies; preserve explicit operator overrides.

- Keep transport state-path resolution independent of installed providers while
  enforcing portable, single-component transport tokens.

- Invoke stop hooks inside their supervised deadline task so call-time errors
  cannot bypass framework cleanup; accept coroutine and Future-backed hooks.

- Separate cold core/native import probes so subprocess-based qualification
  respects test ownership; verify core logger defaults without installed SDKs.
