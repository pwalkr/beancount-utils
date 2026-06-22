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
