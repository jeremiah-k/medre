# Use the fixture CA for explicit certifi clients in Docker campaigns

Point both isolated Python environments' certifi bundles at the private lab CA
so native clients using an explicit certifi context can verify fixture TLS.
Keep network-dependent SDK preloading ahead of the trust switch and leave host
certificate stores untouched.
