# Contributing to bandctl

## Before you start

Bug fixes, tests, and docs can land directly. A new way of enforcing shares (a different qdisc, eBPF, persisting rules after exit) needs a decision first. The current design is described in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

From the repository root, with Textual installed (`pip install -r requirements.txt`):

```
python -m unittest discover -s tests -v
```

The suite does not need root and does not install a qdisc. It will read `/proc` for one smoke test of the process scan.

## Rules for a change

- A bug fix comes with a unit test that failed before the fix.
- Behaviour that the README or `docs/` describe is updated in the same commit.
- Plain, short sentences. Tables for keys and command sequences. No em dashes.

## Commits

- Commit straight to `main`. No force-push.
- One logical change per commit. The message says what changed and why.
- Run the tests before pushing.

The version is `__version__` in `bandctl/__init__.py`. When it changes, add a [CHANGELOG](CHANGELOG.md) entry and update the status line in the README.
