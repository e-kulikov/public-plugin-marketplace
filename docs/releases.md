# Releases

Users install `e-kulikov/public-plugin-marketplace#stable`. Development happens on `main`.
Only the marketplace has GitHub Releases and tags. Plugins are versioned components
inside its snapshot; automation updates registered plugins with local relative sources.
`chatgpt-backup` was migrated with its existing `1.0.0` version. The public
marketplace starts with a fresh `1.0.0` baseline and no private repository history.

## Normal development

Use a branch and Conventional Commits. Merge commits preserve the original messages;
no special PR title or squash is required. If squash is used, its resulting message
must preserve the intended conventional type and breaking-change footer.
Do not manually bump existing versions or edit generated changelogs.

Any delivered plugin change releases at least patch. `feat` releases minor;
`!` or `BREAKING CHANGE:` releases major, including for `0.x` plugins. Other
conventional types (including docs, refactor and tests) release patch.
Unregistered plugin changes and internal CI changes do not release the catalog.

The marketplace compares its last published snapshot with the proposed catalog:

| Change | Marketplace bump |
| --- | --- |
| Removed/renamed plugin or any plugin major increase | major |
| Added plugin or any plugin minor increase | minor |
| Plugin patch increases or catalog metadata/source changes | patch |
| No delivered changes | none |

The largest applicable bump wins. New plugins retain their declared initial version.
Versions use stable numeric SemVer; prereleases are not part of this initial workflow.

## Automation

`scripts/release/run.mjs` uses the pinned release-please library with custom JSON
strategies and a catalog extension. The planner calculates against the latest
reachable `marketplace-v*` tag. One PR updates both plugin manifests, catalog entries,
the marketplace version, `.release-please-manifest.json`, and component changelogs.
Configuration follows registered local entries, so adding a plugin needs no
separate release-tool configuration change.

The validation workflow checks every manifest and the whole catalog, conventional
non-merge commit messages, shell syntax, release tests, and changed-plugin tests.
New sources are checked for reachability. Release PRs also verify the exact computed
catalog and versions, and permit only generated files.

After successful validation, the merge workflow executes trusted `main`, checks
that the release head includes current main, and merges only the exact SHA with a
successful GitHub Actions `validate` check. Ordinary PRs are never auto-merged.
Publication rechecks the merged catalog against its main parent to catch changes
that raced the checked merge; a mismatched batch is not delivered.
If branch protection is available, require `validate` and an up-to-date branch.
The checked merge workflow also works when private-branch protection is unavailable.

The release workflow runs on pushes to main. It first publishes merged release PRs,
then prepares the next release PR. The only tags are `marketplace-v<version>`.
Every release contains the complete repository snapshot and a `plugin-versions.json`
asset listing all included plugins and their versions. Release notes describe only
changed plugins with their included versions and changes, plus catalog changes.
The initial `1.0.0` release keeps the baseline notes. No separate plugin releases
or tags are created. Component changelogs remain part of the snapshot.

`stable` advances only after the marketplace release and inventory are published
and plugin manifests agree with the tagged catalog. Promotion is fast-forward only.
Tags are never moved during normal release operation.
Workflow concurrency serializes release and automatic-merge operations.

## Credentials and recovery

Repository secret `RELEASE_PLEASE_TOKEN` is a GitHub token with Contents, Pull
requests and Issues read/write access to this repo. A fine-grained PAT scoped to
this repo is recommended for future credential rotation. It enables bot PRs and merges
to trigger CI. The merge workflow reads check runs using the built-in GitHub token.
Never print or commit tokens.

The first main run publishes the current `1.0.0` baseline without generating bumps.
Initial publication can be retried: existing tags must match the baseline commit.
Later publications similarly reuse matching tags/releases and mark the PR tagged
after its marketplace release is published. `stable` remains on the last completed batch.

Rerun **Release marketplace** to recover publication or PR generation. Rerun
**Merge checked release PR** to retry a merge. To fix a broken published plugin,
merge a conventional fix; publish a new version rather than moving an old tag.

Local checks: `npm ci --ignore-scripts`, `npm run validate`, `npm test`.
Read-only PR generation preview: `GH_TOKEN=… npm run release:plan` on a committed
checkout after the initial baseline exists. No preview publishes anything.

## Switching existing installations

Existing installations following main must change their source once. In the
client's marketplace manager remove the existing registration and re-add the same
marketplace from `e-kulikov/public-plugin-marketplace#stable`, then verify installed plugins.
Users pinned to a specific `#marketplace-v1.0.0` tag remain on that snapshot.
Automatic updates are configured in the client, not enabled by a GitHub Release.
