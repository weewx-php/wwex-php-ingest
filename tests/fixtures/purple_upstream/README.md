# Upstream PurpleAir test fixture

`purple.py` is an unmodified copy of chaunceygardiner/weewx-purple 7.2 at
commit `e433d858c33c9f5c2e0083f27baa48f190d9feb8`:

https://github.com/chaunceygardiner/weewx-purple/blob/e433d858c33c9f5c2e0083f27baa48f190d9feb8/bin/user/purple.py

SHA-256: `e85c13f459aad83c146d4aa1e3bc52e8b214a8b8e9638e3fcd476b119fd7005f`

Copyright John A Kline. GPL-3.0; original license in `LICENSE`.
Used only by tests, never installed as part of the collector package. Tests run
the original service in isolated worker processes against local fake sensors.
