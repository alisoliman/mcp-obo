# Pull request

## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- The constraint or failure that made the change necessary. If it fixes an
     issue, link it here with "Fixes #123". -->

## Checks

- [ ] `uv run pytest` passes
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] `az bicep build --file infra/main.bicep --stdout > /dev/null` succeeds, if
      `infra/` changed
- [ ] Tests cover the new behaviour, or the change is not testable and the
      description says why
- [ ] Docs updated, including [AGENTS.md](../AGENTS.md) if the file tree or a
      change procedure moved

## Deployment impact

<!-- Does this need a re-provision (infra/ or azure.yaml) or only a re-deploy
     (application code)? Does it add an azd environment variable? Does it change
     what either workload is permitted to do? -->
