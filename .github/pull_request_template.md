## What and why

<!-- One change per pull request. -->

## Checklist

- [ ] Conventional commit title (`feat:`, `fix:`, `docs:` …): it becomes the changelog entry
- [ ] Tests for the change; `pytest` passes
- [ ] `ruff check && ruff format --check && mypy custom_components tests` clean
- [ ] User-facing changes are in `docs/` and, for strings, in both `strings.json` and `translations/en.json`
- [ ] No serials, account ids, e-mails, addresses, device names or captures in the diff
