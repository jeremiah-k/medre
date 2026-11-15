# Timestamp runtime console output

Runtime lifecycle summaries follow the configured log level and text/JSON
format. Text logs prefix every physical line, including traceback
continuations, with the event timestamp, severity, and logger name. JSON
timestamps reflect record creation in UTC rather than formatting time.
Repeated logging setup follows redirected stdout without retaining a closed
stream or adding duplicate console handlers.

Unexpected core startup exceptions emit a redacted ERROR record in the configured
text or JSON format, including when INFO output is filtered.
