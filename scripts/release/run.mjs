import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {Manifest, GitHub} from 'release-please';
import {CATALOG, VERSION_MANIFEST, pluginPath, pluginInventory, BASELINE_NOTES} from './policy.mjs';
import {baselineTag, buildPlan, git, jsonAt, readJson} from './plan.mjs';
import {manifestConfiguration, parsedVersions, registerAdapter} from './adapter.mjs';

const repo = process.env.GITHUB_REPOSITORY ?? 'e-kulikov/public-plugin-marketplace';
const [owner, repository] = repo.split('/');
const command = process.argv[2];
const gh = (...args) => execFileSync('gh', args, {encoding: 'utf8'}).trim();
const api = (path, ...args) => JSON.parse(gh('api', path, ...args));
const sha = git('rev-parse', 'HEAD');
const catalog = readJson(CATALOG);
const fetchTags = () => git('-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential', 'fetch', 'origin', '--tags');

function publishedRelease(tag) {
  try {
    const release = api(`repos/${repo}/releases/tags/${encodeURIComponent(tag)}`);
    return !release.draft && !release.prerelease;
  } catch (error) {
    if (String(error.stderr).includes('HTTP 404')) return false;
    throw error;
  }
}

function ensureMain() {
  assert.equal(api(`repos/${repo}/branches/main`).commit.sha, sha,
    'Checkout is stale; rerun the workflow against current main.');
}

async function releaseManifest(plan) {
  registerAdapter(plan);
  const github = await GitHub.create({owner, repo: repository,
    token: process.env.GH_TOKEN ?? process.env.GITHUB_TOKEN});
  return new Manifest(github, 'main', manifestConfiguration(catalog), parsedVersions(readJson(VERSION_MANIFEST)), {
    plugins: ['marketplace-catalog'], separatePullRequests: false,
    manifestPath: VERSION_MANIFEST, bootstrapSha: plan.baselineSha,
    sequentialCalls: true,
  });
}

function ensureRelease(tag, target, notes, latest = false) {
  const refs = api(`repos/${repo}/git/matching-refs/tags/${tag}`).filter(ref => ref.ref === `refs/tags/${tag}`);
  if (refs.length) {
    assert.equal(refs[0].object.sha, target, `Existing tag ${tag} points at another commit`);
  } else {
    api(`repos/${repo}/git/refs`, '-X', 'POST', '-f', `ref=refs/tags/${tag}`, '-f', `sha=${target}`);
  }
  if (!publishedRelease(tag)) {
    gh('release', 'create', tag, '--repo', repo, '--verify-tag', '--title', tag,
      '--notes', notes, `--latest=${latest}`);
  }
  const directory = mkdtempSync(join(tmpdir(), 'marketplace-inventory-'));
  try {
    const asset = join(directory, 'plugin-versions.json');
    writeFileSync(asset, `${JSON.stringify(pluginInventory(jsonAt(target, CATALOG)), null, 2)}\n`);
    gh('release', 'upload', tag, asset, '--repo', repo, '--clobber');
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}

function promoteStable(tag) {
  fetchTags();
  const target = git('rev-parse', `${tag}^{commit}`);
  const snapshot = jsonAt(target, CATALOG);
  const versions = jsonAt(target, VERSION_MANIFEST);
  for (const entry of snapshot.plugins) {
    const path = pluginPath(entry);
    if (!path) continue;
    assert.equal(versions[path], entry.version);
    for (const client of ['.claude-plugin', '.codex-plugin']) {
      assert.equal(jsonAt(target, `${path}/${client}/plugin.json`).version, entry.version);
    }
  }
  assert(publishedRelease(tag), `Missing release ${tag}`);
  const refs = api(`repos/${repo}/git/matching-refs/heads/stable`).filter(ref => ref.ref === 'refs/heads/stable');
  if (refs.length) {
    // GitHub refuses non-fast-forward promotion. Never roll stable backwards.
    api(`repos/${repo}/git/refs/heads/stable`, '-X', 'PATCH', '-f', `sha=${target}`, '-F', 'force=false');
  } else {
    api(`repos/${repo}/git/refs`, '-X', 'POST', '-f', 'ref=refs/heads/stable', '-f', `sha=${target}`);
  }
  console.log(`Delivered ${tag} at ${target}`);
}

if (command === 'plan') {
  const plan = buildPlan();
  const manifest = await releaseManifest(plan);
  const candidates = await manifest.buildPullRequests();
  console.log(JSON.stringify({baseline: plan.baseline, level: plan.level, catalog: plan.catalog,
    versions: plan.versions, pullRequests: candidates.map(pr => ({title: pr.title.toString(),
      files: pr.updates.map(update => update.path)}))}, null, 2));
} else if (command === 'pr') {
  ensureMain();
  const manifest = await releaseManifest(buildPlan());
  console.log(JSON.stringify(await manifest.createPullRequests()));
} else if (command === 'publish') {
  ensureMain();
  if (!baselineTag()) {
    const initialCatalog = jsonAt(sha, CATALOG);
    assert.equal(initialCatalog.metadata.version, '1.0.0');
    const versions = jsonAt(sha, VERSION_MANIFEST);
    for (const version of Object.values(versions)) assert.equal(version, '1.0.0');
    ensureRelease('marketplace-v1.0.0', sha, BASELINE_NOTES, true);
  } else {
    const manifest = await releaseManifest(buildPlan());
    const candidates = await manifest.buildReleases();
    for (const candidate of candidates.filter(candidate => candidate.path === '.')) {
      // Catch a main update racing the checked merge when server-side strict
      // branch protection is unavailable. Never deliver an under-versioned batch.
      const parent = git('rev-parse', `${candidate.sha}^1`);
      const expected = buildPlan(parent);
      assert.deepEqual(jsonAt(candidate.sha, CATALOG), expected.catalog,
        'Merged release no longer matches main; regenerate the release before publication.');
    }
    assert(candidates.every(candidate => candidate.path === '.'), 'Only marketplace releases may be published');
    for (const candidate of candidates) {
      ensureRelease(candidate.tag.toString(), candidate.sha, candidate.notes, candidate.path === '.');
    }
    for (const number of new Set(candidates.map(candidate => candidate.pullRequest.number))) {
      api(`repos/${repo}/issues/${number}/labels`, '-X', 'POST', '-f', 'labels[]=autorelease: tagged');
      gh('api', `repos/${repo}/issues/${number}/labels/autorelease%3A%20pending`, '-X', 'DELETE');
    }
  }
  fetchTags();
  const tag = baselineTag();
  // Recover if creation of the aggregate tag succeeded but its release failed.
  const target = git('rev-parse', `${tag}^{commit}`);
  const notes = tag === 'marketplace-v1.0.0' ? BASELINE_NOTES :
    git('show', `${target}:CHANGELOG.md`).split(/(?=^## )/m)[1].trim();
  ensureRelease(tag, target, notes, true);
  promoteStable(tag);
  // Only the marketplace is shown as the repository's latest release.
  gh('release', 'edit', tag, '--repo', repo, '--latest');
} else if (command === 'merge') {
  ensureMain();
  const pulls = api(`repos/${repo}/pulls?state=open&base=main&per_page=100`);
  for (const pull of pulls) {
    if (pull.head.ref !== 'release-please--branches--main' || pull.head.repo?.full_name !== repo ||
        !pull.labels.some(label => label.name === 'autorelease: pending')) continue;
    // Require current main to be an ancestor of the checked release commit.
    const comparison = api(`repos/${repo}/compare/${sha}...${pull.head.sha}`);
    if (comparison.status !== 'ahead') continue;
    const token = process.env.CHECKS_TOKEN;
    assert(token, 'CHECKS_TOKEN must provide checks:read');
    const checkedApi = path => JSON.parse(execFileSync('gh', ['api', path], {
      encoding: 'utf8', env: {...process.env, GH_TOKEN: token},
    }));
    const checks = checkedApi(`repos/${repo}/commits/${pull.head.sha}/check-runs?per_page=100`).check_runs;
    const validation = checks.filter(check => check.name === 'validate' && check.app?.slug === 'github-actions')
      .sort((a, b) => b.id - a.id)[0];
    if (!validation || validation.status !== 'completed' || validation.conclusion !== 'success') continue;
    // Bind merge to the exact checked head, even if the bot updates its branch.
    api(`repos/${repo}/pulls/${pull.number}/merge`, '-X', 'PUT', '-f', `sha=${pull.head.sha}`,
      '-f', 'merge_method=merge', '-f', 'commit_title=chore: merge release marketplace');
    console.log(`Merged release PR #${pull.number}`);
  }
} else {
  throw new Error('Usage: node scripts/release/run.mjs plan|pr|publish|merge');
}
