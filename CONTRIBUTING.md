# Contributing

This project welcomes contributions and suggestions. Most contributions require
you to agree to a Contributor License Agreement (CLA) declaring that you have
the right to, and actually do, grant us the rights to use your contribution. For
details, visit <https://cla.opensource.microsoft.com>.

When you submit a pull request, a CLA bot will automatically determine whether
you need to provide a CLA and decorate the PR appropriately. You only need to do
this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of
Conduct](CODE_OF_CONDUCT.md).

- [Setting up](#setting-up)
- [Running the checks](#running-the-checks)
- [Code style](#code-style)
- [What a change usually touches](#what-a-change-usually-touches)
- [Submitting a pull request](#submitting-a-pull-request)

## Setting up

```bash
uv sync
uv run pre-commit install
npm --prefix frontend ci
```

`uv sync` installs the runtime dependencies and the `dev` group, which is where
`pytest` and `ruff` live. [docs/localdev.md](docs/localdev.md) covers what you
can run without deploying, and what genuinely needs an `azd up` first.

## Running the checks

These are the same commands CI runs, so a red build is reproducible in a second:

```bash
uv run pytest                                          # unit tests, no Azure
uv run ruff check .                                    # lint
uv run ruff format --check .                           # formatting
uvx mypy==2.3.0 --python-executable .venv/bin/python   # types
az bicep build --file infra/main.bicep --stdout > /dev/null
npm --prefix frontend run build && npm --prefix frontend run lint
```

mypy runs isolated rather than from the `dev` group — it is a tool, not
something this code imports, and keeping it out of the dependency graph stops it
constraining resolution. `--python-executable` is what lets it read the
project's installed packages, so pydantic and httpx types are actually checked.
It covers `src` and `scripts`; `tests` is excluded, because checking fakes
produces noise about the mocks rather than findings about the code.

Against a live deployment, after `azd up`:

```bash
azd provision --preview --no-prompt
uv run pytest tests/e2e_databricks.py     # databricks mode only
```

`tests/e2e_databricks.py` does not match pytest's `test_*.py` collection
pattern, which is what keeps a bare `pytest` from calling your deployed stack.
Run it by naming it.

## Code style

`ruff` decides formatting and import order; do not argue with it, run it.

Beyond that, the convention this repo does hold to is about comments. A comment
here explains why the code is the shape it is -- which constraint, which
provider behaviour, which failure it is avoiding. It does not restate what the
next line does. If a comment could be deleted without losing a fact that is not
in the code, delete it.

Claims in prose have to be checkable. `tests/test_infrastructure.py` asserts
against the compiled ARM template rather than trusting the README's account of
what permissions are requested, and `tests/test_contracts.py` asserts that the
tool names the chat app allows are the ones the server registers. New claims
should arrive with the test that holds them.

## What a change usually touches

[AGENTS.md](AGENTS.md) has the annotated file tree and step-by-step procedures
for the changes that span several files -- adding an azd environment variable,
adding a new MCP mode, upgrading dependencies. Read the relevant section before
starting; every one of those chains has a link that is easy to miss.

## Submitting a pull request

- Search the open and closed pull requests first, so you do not duplicate work.
- Make the change on a branch in your fork.
- Run the checks above, and add tests for what you changed.
- Write a commit message that says what changed and why.
- Open the pull request against `main` and ask a maintainer to review it.
