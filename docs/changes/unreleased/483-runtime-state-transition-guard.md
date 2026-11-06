# Validate process lifecycle transitions before publishing them

Declare and enforce the process-level RuntimeState transition graph, distinct
from adapter states and receipt/outbox state machines. Illegal transitions
leave both process state and the observable event stream unchanged; stopped
runtime instances cannot be reopened by late asynchronous failures. Keep
startup/shutdown ordering and durable-delivery authorities unchanged.
