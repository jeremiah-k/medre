# Bound native dispatch shutdown without releasing live ownership

Outbox dispatch stop shares the runtime drain deadline with ingress and delivery.
If a native operation resists cancellation, shutdown returns a failure and retains
pipeline, adapters and storage until that operation exits. Calling stop again can
then finish cleanup. Cooperative cancellation retains the durable reservation
without inventing accepted delivery evidence or making an uncertain send unsent.
