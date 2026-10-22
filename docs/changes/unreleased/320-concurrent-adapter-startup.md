# Start independent adapters concurrently

Own adapter startup tasks in one structured group after core preparation.
Slow connections and failed-start cleanup do not delay independent starts.
Keep ingress, retry and typing activation behind the completion barrier, retain
stable summaries and reverse identifier shutdown order, and roll back partial
and ready adapters together when startup is cancelled.
