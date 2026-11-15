# Report explicit synthetic lifecycle drill assignments

Replace positional transport cycling with explicit adapter-instance assignments
for programmatic configuration/startup drills. Use the neutral `source` ID in
the bad-route fixture. Reports expose `adapter_transports` and
`coverage_scope: synthetic_adapter_instances`, and state that cross-transport
coverage is not proven.

These drills intentionally use one installed fake provider. Three configured
instances still construct three adapters and exercise partial startup with two
successes and one failure. With no provider installed, return a failed report
with an installation reason instead of raising out of the runner.
