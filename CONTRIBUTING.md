# Contributing

Keep privacy behavior explicit and reproducible. Explain the concrete trigger,
the changed result and the relevant capture path. Add boundary or regression
coverage for meaningful failures; avoid tests that only mirror implementation.

Run the offline checks from the checkout:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
make test-session-guard test-png-guard test-privacy-policy test-spoiler-pattern test-appearance test-native-config
```

Native render, hook, ownership or policy changes also need the relevant
isolated compositor regressions in [TESTING.md](docs/TESTING.md). Build against
the exact supported headers and record source/binary hashes. CI runs offline
logic tests; it does not substitute for pixel captures or GL lifecycle checks.

Use synthetic fixtures. Do not add personal screenshots, private backups,
runtime state, compiled binaries or generated reports to the repository.
`artifacts/`, `build/` and local source references are ignored. A failed report
stays failed; a pass requires all assertions and confirmed cleanup.

When changing a compositor target, review exact symbols, argument ownership,
native cached paths and teardown before extending admission. Keep online
predecessor lists explicit; do not replace exact hashes with a wildcard.
Preserve third-party notices when adapting upstream definitions.

Report suspected privacy disclosures using [SECURITY.md](SECURITY.md).
