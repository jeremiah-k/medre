# MeshCore firmware text limits

MeshCore rendering reserves the firmware-added device name and separator within
the 160-byte group text limit. Planning advertises 127 bytes until the device name
is known, then derives its effective budget from live device information. The
operator text limit defaults to 160 and remains an upper bound.

Truncation occurs at UTF-8 boundaries before native delivery and is recorded in
rendering evidence. Oversized direct session sends fail permanently before SDK
I/O, avoiding both silent firmware clipping and retries of oversized BLE frames.
