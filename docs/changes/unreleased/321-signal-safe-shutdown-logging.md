# Keep shutdown logging outside signal handlers

SIGINT and SIGTERM handlers set the shutdown request flag without logging or
stream I/O. The normal lifecycle emits shutdown progress. A signal arriving
during console output cannot recursively flush the same buffered stream or
produce a logging-error traceback.
