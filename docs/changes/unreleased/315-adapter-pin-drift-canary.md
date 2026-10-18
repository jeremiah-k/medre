# Scheduled adapter pin-drift canary

- Add a daily 08:30 UTC (plus manual dispatch) workflow that clones every
  `.github/adapter-compatibility.json` repository at its default-branch HEAD
  and runs `scripts/ci/adapter_matrix.py --check-local` against those clones,
  failing when a manifest pin lags an adapter default branch.
- On drift the job summary names the two legitimate resolutions: a Renovate
  pin bump once cross-adapter CI qualifies the pair, or a coordinated
  core-first flow where the pin waits on an unmerged core PR. The canary only
  reports; nothing syncs pins automatically.

Unique repositories use owner-scoped checkout paths. Comparison requires full
commit pins, overlapping runs queue, and prerequisite failures produce a
verification-error summary rather than a false pin-drift diagnosis.
