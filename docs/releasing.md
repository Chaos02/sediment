# Releasing Sediment

For maintainers: how a tag becomes a release, and the one-time setup that has to
exist before the first tag.

## How the pieces fit

- The project lives on GitLab: `gitlab.com/Chaos02-oss/sediment`. Code, issues,
  merge requests, CI, tags and releases are all there.
- `github.com/Chaos02/sediment` is a copy and nothing else. The Home Assistant
  Marketplace (HACS) can only install from GitHub, and it takes versions from
  GitHub Releases. The copy has issues turned off, no `.github/` directory and no
  Actions.
- A GitLab push mirror copies branches and tags to GitHub.
- The tag pipeline creates the release on both sites. It creates the GitHub
  Release through the API, after the mirror has delivered the tag.

## Versions and tags

| Tag | Meaning | Version in `manifest.json` | GitHub Release |
| --- | --- | --- | --- |
| `v0.1.0-beta.1` | Beta | `0.1.0-beta.1` | Marked as pre-release |
| `v0.1.0` | Final | `0.1.0` | Normal release |

The pipeline refuses any other tag shape. The tag version must equal the version in
`custom_components/sediment/manifest.json` exactly (`0.1.0-beta.1`).

Release notes come from `CHANGELOG.md`:

- A final release needs a section `## 0.1.0 - 2026-12-01` (a bracketed
  `## [0.1.0]` works too). Without it, the pipeline refuses the release. The text of
  the section becomes the release notes.
- A beta uses the `## Unreleased` section as notes. If that section is empty, the
  release gets a one-line note.

So changes collect under Unreleased while betas go out. For the final release,
rename Unreleased to the version and start a new empty Unreleased above it.

Users get betas only when they turn on the "Pre-release" switch for Sediment in
HACS or the Marketplace.

## Cutting a release

A beta, a final release and the first publish each need the maintainer's go.
Batch releases. Tag when a meaningful set of changes has collected, or for a
security fix.

1. Set `version` in `manifest.json` and update `CHANGELOG.md` as described above.
2. Commit with a signed commit and merge to `main`. Wait for the pipeline of `main`
   to turn green.
3. Create a signed tag on that commit and push it:

   ```
   git tag -s v0.1.0-beta.1 -m "Sediment 0.1.0-beta.1"
   git push origin v0.1.0-beta.1
   ```

4. Watch the tag pipeline. When the release job is done, the release exists on
   GitLab and on GitHub.

The tag has to be protected (see setup), and its commit has to be on `main`.

## What the pipeline runs

| Job | When | What it does |
| --- | --- | --- |
| `tag-check` | tags | Tag shape, tag equals manifest version, tag commit is on `main`, CHANGELOG section for finals |
| `lint` | all | `ruff check` and `ruff format --check`, with the ruff version from `requirements_test.txt` |
| `hassfest` | all | Home Assistant's own validation of `custom_components/sediment`, in the image the hassfest GitHub Action uses |
| `hacs-check` | all | `scripts/check_hacs.py`: `hacs.json`, one integration, manifest keys, `brand/` icon, README |
| `test-ha-current` | all | pytest on the Home Assistant version pinned in `requirements_test.txt` |
| `test-ha-previous` | all | The same tests on the previous Home Assistant minor. May fail without failing the pipeline, because `hacs.json` asks for the newer minor |
| `release` | tags | Builds `sediment.zip`, creates the GitLab release, waits for the mirror, creates the GitHub Release |

Pipelines run for merge requests, pushes to `main` and protected tags. A change
that only touches docs, README or CHANGELOG starts no pipeline.

### Why the HACS check is a script

The official HACS validation (the `hacs/action` container) reads the repository
through the GitHub API and only accepts a public GitHub repository. The pipeline
runs on GitLab before anything reaches GitHub, so it cannot use it.
`scripts/check_hacs.py` checks the same file rules locally. The checks that need
GitHub itself (issues enabled, repository description, topics) do not apply to a
mirror, so the script skips them. A missing `brand/icon.png` is a warning, and an
error for a final release tag.

### The zip

`sediment.zip` holds the contents of `custom_components/sediment` at its root. Both
releases carry it. Use it to install by hand: unpack it into
`<config>/custom_components/sediment`. HACS does not use it. `hacs.json` has no
`zip_release`, so HACS installs from the tagged source. That is simpler and cannot
break on a missing asset. The release job builds the zip the same way every time,
so running the job again produces the same bytes.

## Holding a release

To run every check but park the release step until you say go:

```
git push -o ci.variable="PUBLISH_HOLD=1" origin v0.1.0-beta.1
```

The `release` job then waits as a manual job. Pressing Play on it is the release.
Setting `PUBLISH_HOLD` as a project variable holds every release until you remove it.

## Running the release job again

You can run the job again. It skips a package file with the same content. It
leaves an existing GitLab release alone, updates an existing GitHub Release and
keeps an identical asset. If a package file exists with different content, the job
stops. A published version never changes, so release the next version.

If the job times out waiting for the tag on GitHub, look at the push mirror in
GitLab (Settings, Repository, Mirroring repositories). Press the update button
there, then run the job again.

## One-time setup

Do these in order. The tag rule and the variable must exist before the first tag:
a protected variable is only visible to pipelines for protected tags.

### 1. GitHub: the repository

1. Create `Chaos02/sediment`, public, empty (no README, no licence, no `.gitignore`).
2. Description: `Mirror of gitlab.com/Chaos02-oss/sediment`. Website:
   `https://gitlab.com/Chaos02-oss/sediment`.
3. Settings, General, Features: turn off Issues, Wikis, Projects and Discussions.
4. Settings, Actions, General: choose "Disable actions".
5. Do not create releases by hand and do not push to this repository.

### 2. GitHub: the token

Settings, Developer settings, Personal access tokens, Fine-grained tokens, Generate:

- Resource owner: `Chaos02`.
- Repository access: only `Chaos02/sediment`.
- Repository permissions: Contents, Read and write. Nothing else (GitHub adds
  Metadata read by itself).
- Expiry: pick a date and put it in your calendar. When the token expires, mirror and
  releases stop.

One token serves both the mirror and the release job.

### 3. GitLab: the protected tag rule

Settings, Repository, Protected tags: add the wildcard `v*`. Allowed to create:
Maintainers. Do this before the first tag.

### 4. GitLab: the CI variable

Settings, CI/CD, Variables, Add variable:

- Key: `GITHUB_RELEASE_TOKEN`
- Value: the token from step 2
- Environments: `release` (the environment scope; only the release job uses it, so
  the lint, test and validation jobs never see the token)
- Flags: Protect variable on, Mask variable on, Expand variable reference off

### 5. GitLab: the push mirror

Settings, Repository, Mirroring repositories:

- Git repository URL: `https://github.com/Chaos02/sediment.git`
- Mirror direction: Push
- Authentication method: Password
- Username: `Chaos02`
- Password: the token from step 2
- Keep divergent refs: off (GitHub is write only, GitLab always wins)
- Only mirror protected branches: optional. GitLab's documentation does not say
  whether tags are pushed when this is on, so leave it off unless you want fewer
  branches on GitHub, and then check that a tag arrives.

Press "Update now". The branch `main` should appear on GitHub within a few minutes.

### 6. Check it

Push a first beta tag with `PUBLISH_HOLD` set, look at the checks, then press Play
on the release job. Check that the GitHub Release is marked as pre-release and has
`sediment.zip` attached.

Then add the repository in HACS or the Marketplace by its GitHub address, with
pre-releases turned on, and install the beta.

## Later: the HACS default list

The default list needs issues enabled on GitHub and the official HACS action
passing on the GitHub repository. Ask the HACS team first whether a mirror with a
template that points to GitLab is accepted. Until then users add the repository
by hand.
