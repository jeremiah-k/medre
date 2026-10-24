# Qualify the durable Matrix adapter compatibility pin

- Pin the Matrix adapter's durable mindroom-nio integration, requiring Python
  3.12+ for that provider while retaining Python 3.11 support in core.
- Generate adapter contract jobs and full-provider compatibility lanes from
  provider minimum Python minor versions in the CI snapshot. Keep an explicit
  adapter-free Python 3.11 lane and compatible provider contract jobs.
