# Classify startup using settled native health

- Recheck started adapter health concurrently after independent startup attempts
  settle, under the existing bounded and cancellable startup ownership. Use the
  fresh observations for startup health and route readiness.
- Report live-eligible and startup-blocked expanded routes separately from
  build registration. Count retained router entries after destination pruning
  in the boot summary, including failed-source routes retained for stored work.
- Name degraded adapters in the startup summary. Make the separate live health
  command include the selected config and explain that the bridge must stop
  before the probe opens its own native sessions.
- Count successfully stopped started adapters in the shutdown summary rather
  than all configured adapters, including those which never started.
