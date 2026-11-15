# Bound independent adapter startup

runtime.adapter_start_timeout_seconds sets a cooperative connection deadline for
each concurrently started adapter (60 seconds by default). An expired start is
cancelled, its partial resources are retired, and healthy peers enter the runtime
without waiting indefinitely. Provider-native timeout diagnoses are preserved.
Native threads require SDK cancellation support or a worker-process boundary;
this coroutine deadline does not terminate them.
