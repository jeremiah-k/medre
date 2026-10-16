# Retain Matrix events received during startup

Pin the Matrix provider fix for events delivered by Classic Sync before adapter
startup returns. Messages sent while MEDRE was offline now reach durable ingress
instead of being discarded while the SDK advances its checkpoint. Initial history
is admitted with routing suppressed, and inactive durable callbacks explicitly
defer acceptance. Startup and auto-join failure or cancellation close the admission
window and clean up the session.
