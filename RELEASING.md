# Releasing loopd

Publishing to [PyPI](https://pypi.org/project/loopd/) is automated. Pushing a version tag runs
the tests, builds the package, and publishes it — no laptop, no manual `twine`, no tokens on your
machine. Auth is **PyPI Trusted Publishing (OIDC)**: PyPI trusts this exact repo + workflow, so
there is no API token or secret to store, leak, or rotate.

The workflow is [`.github/workflows/release.yml`](.github/workflows/release.yml).

## One-time setup (do this once, ever)

### 1. Register the trusted publisher on PyPI
Since `loopd` already exists on PyPI:

1. Go to <https://pypi.org/manage/project/loopd/settings/publishing/>.
2. Under **Add a new publisher → GitHub**, enter exactly:
   - **Owner:** `ruchirk22`
   - **Repository name:** `loopd`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
3. Save. PyPI will now accept uploads that come from this repo's `release.yml` running in the
   `pypi` environment — and nothing else.

> If you ever transfer the repo or rename the workflow, update this publisher to match.

### 2. (Recommended) Create the `pypi` GitHub environment
This adds an approval gate so a tag can't publish without your click.

1. Repo → **Settings → Environments → New environment** → name it `pypi`.
2. (Optional) Add yourself under **Required reviewers**, and restrict **Deployment branches and
   tags** to tags matching `v*`.

The `publish` job already targets `environment: pypi`, so this just turns on the guard rails.

## Every release

1. **Bump the version** in `pyproject.toml` (e.g. `0.1.3` → `0.1.4`).
2. **Update `CHANGELOG.md`** — move the `[Unreleased]` items under a new `## [0.1.4] - YYYY-MM-DD`.
3. **Commit** on `main` (via PR or directly): `git commit -am "release: 0.1.4"`.
4. **Tag and push** — the tag must be `v` + the exact pyproject version:
   ```bash
   git tag v0.1.4
   git push origin main --tags
   ```
5. Watch **Actions → Release**. It will:
   - run the full test matrix on the tagged commit,
   - assert the tag matches the `pyproject` version (a mismatch fails the run before publishing),
   - build the wheel + sdist, `twine check`, and confirm prompts/assets are bundled,
   - publish to PyPI (pausing for your approval first, if you enabled the `pypi` environment gate).
6. Confirm: <https://pypi.org/project/loopd/> shows the new version; `pip install -U loopd` gets it.

## If something goes wrong

- **Tag/version mismatch** → the `build` job fails with a clear message. Delete the tag
  (`git tag -d v0.1.4 && git push origin :refs/tags/v0.1.4`), fix the version, re-tag.
- **PyPI rejects the upload** → PyPI versions are immutable; you can never re-upload the same
  version. Bump to the next patch and tag again.
- **Tests fail on the tag** → nothing is published. Fix on `main`, then re-tag.
