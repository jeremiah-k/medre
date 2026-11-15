# Portable core and Docker contract verification

- Read Python and Markdown sources as UTF-8, including on Windows where the
  locale may default to CP1252. Normalize source-audit relative paths to POSIX
  separators and test the recovery CLI against its shell-quoted file paths.
- Confine POSIX shell-fixture checks to POSIX test hosts. Windows still runs
  the full adapter-free Python suite and reports its slowest tests.
- Run Docker integration and artifact scripts on macOS without GNU `timeout`;
  prefer `gtimeout` where installed, otherwise rely on per-test/collector and
  CI job deadlines.
- Prevent the Docker bridge artifact collector's valid scenarios from falling
  through Bash's `case` catch-all branch. Validate timeout input before it is
  interpolated into Python and use the selected Python interpreter for pytest.
- Exercise Matrix E2EE capability on native Windows CI using the installed
  `mindroom-nio[e2e]` fork and actual crypto-store/cross-signing SDK contracts.
