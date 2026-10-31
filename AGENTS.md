# AGENTS.md

Guidance for AI agents working in this repository.

## Tests

Tests live in [`tests/`](tests/) and use [pytest](https://docs.pytest.org/)
(`testpaths` is set in `pyproject.toml`, so a bare `pytest` discovers them).

Install dependencies, then run the suite:

```sh
pip install -r requirements.txt
pytest
```

Useful invocations:

```sh
pytest                              # run everything
pytest tests/test_decorator.py      # a single module
pytest -k balances                  # tests matching an expression
pytest -q                           # quiet output
```

## Conventions

- Write tests in modern pytest style: plain functions, bare `assert`,
  fixtures, and `@pytest.mark.parametrize` — not `unittest.TestCase`.
- Keep new tests under `tests/`.

## Deduplication

- Importers mark duplicates in `deduplicate(entries, existing)`, mostly via
  `beancount_utils.deduplicate.mark_duplicate_entries`.
- Pass `id_key` when the source provides stable transaction ids and the
  importer stores them in posting meta (e.g. `singlefin` uses
  `simplefin_id`). Equal ids decide a match outright; postings without the
  key fall back to amount/date matching, so older ledger entries still dedupe.
- beangulp's `extract` appends each file's entries to `existing` before
  deduplicating the next file, so `existing` is not just the ledger. Code
  that treats `existing` as ground truth (e.g. `extract_out_of_place`) sees
  earlier files' output too.

## Commits

- Use [Conventional Commits](https://www.conventionalcommits.org/):
  `type(scope): summary`, e.g. `feat(kraken): output price for sales`.
  Common types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
- The scope is the module or importer touched: `deduplicate`, `decorator`,
  `singlefin`, `kraken`, `fidelity-pdf`, `merrill-pdf`, …
- Split independent changes into separate commits. A shared helper and the
  importer that starts using it are separate commits, helper first.
- Commit updates to this file separately as `docs(agents): …`.
- Only stage files you changed. Untracked files such as `dump.txt` (raw
  statement text with personal data) must never be committed.
