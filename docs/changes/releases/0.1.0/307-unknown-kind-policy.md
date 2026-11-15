# 307: Unknown event kinds never route through wildcards

Breaking pre-release change: capability planning suppresses unrecognized event
kinds instead of delivering them through wildcard routes. Known passthrough kinds
remain explicit, including `plugin.custom`; unknown ingress evidence can still
be retained without claiming native delivery support.
