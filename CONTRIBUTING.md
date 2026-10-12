# Contributing

Pull requests are welcome. The maintainer reads issues.

Use Python 3.9 or later. The plugin and its tests use the standard library only.

```sh
nice -n 10 python3 -m unittest discover -s tests
```

Use synthetic transcripts in tests. Do not put your session logs, private paths or message text in an issue or a pull request. A fix for a false alarm needs a regression test that fails without the fix.

Before a pull request can be merged, add the CLA comment shown in [CLA.md](CLA.md).
The code is under GPL-3.0-only. See [LICENSE](LICENSE).
