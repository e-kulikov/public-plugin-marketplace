import test from 'node:test';
import assert from 'node:assert/strict';
import {Manifest} from 'release-please';
import {buildStrategy} from 'release-please/build/src/factory.js';
import {parseConventionalCommits} from 'release-please/build/src/commit.js';
import {TagName} from 'release-please/build/src/util/tag-name.js';
import {Version} from 'release-please/build/src/version.js';
import {Merge} from 'release-please/build/src/plugins/merge.js';
import {registerAdapter, manifestConfiguration, parsedVersions} from './adapter.mjs';

const quiet = {info() {}, debug() {}, warn() {}, error() {}, trace() {}};

test('actual release-please generates component updates but publishes only marketplace with changed-plugin notes', async () => {
  const catalog = {name: 'test', metadata: {version: '1.1.0'}, plugins: [
    {name: 'a', source: './plugins/a', version: '1.2.0'},
    {name: 'b', source: './plugins/b', version: '1.0.0'},
  ]};
  const plan = {baseline: 'marketplace-v1.0.0', baselineSha: 'abc', level: 'minor', catalog, versions: {'plugins/a': '1.2.0'},
    changes: ['a: 1.0.0 → 1.2.0'], commits: {'plugins/a': [{sha: 'def', message: 'feat: new feature'}]}};
  registerAdapter(plan);
  const github = {repository: {owner: 'test', repo: 'marketplace'}, logger: quiet};
  const config = manifestConfiguration(catalog);
  const manifest = new Manifest(github, 'main', config, parsedVersions({'.': '1.0.0', 'plugins/a': '1.0.0'}),
    {plugins: ['marketplace-catalog'], separatePullRequests: false, logger: quiet});
  const strategies = {}, commits = {}, releases = {};
  for (const [path, options] of Object.entries(config)) {
    strategies[path] = await buildStrategy({...options, github, path, targetBranch: 'main', logger: quiet});
    commits[path] = [];
    releases[path] = {tag: new TagName(Version.parse('1.0.0'), options.component, '-'), sha: 'abc', notes: ''};
  }
  await manifest.plugins[0].preconfigure(strategies, commits, releases);
  const candidates = [];
  for (const path of Object.keys(config)) {
    const pullRequest = await strategies[path].buildReleasePullRequest(parseConventionalCommits(commits[path], quiet), releases[path]);
    if (pullRequest) candidates.push({path, config: config[path], pullRequest});
  }
  assert.equal(candidates.length, 2, 'unchanged b must not release');
  const processed = await manifest.plugins[0].run(candidates);
  const merge = new Merge(github, 'main', config);
  const [combined] = await merge.run(processed);
  const pr = combined.pullRequest;
  const files = pr.updates.map(update => update.path);
  assert.equal(files.filter(path => path === '.release-please-manifest.json').length, 1);
  assert.equal(files.filter(path => path === '.claude-plugin/marketplace.json').length, 1);
  assert(!files.some(path => path.startsWith('plugins/b/')));
  const updatedCatalog = pr.updates.find(update => update.path === '.claude-plugin/marketplace.json').updater.updateContent('{}');
  assert.deepEqual(JSON.parse(updatedCatalog), catalog);
  const updatedManifest = pr.updates.find(update => update.path === '.release-please-manifest.json').updater.updateContent('{}');
  assert.deepEqual(JSON.parse(updatedManifest), {'.': '1.1.0', 'plugins/a': '1.2.0', 'plugins/b': '1.0.0'});
  const updatedPlugin = pr.updates.find(update => update.path === 'plugins/a/.claude-plugin/plugin.json').updater.updateContent('{"name":"a","version":"1.0.0"}');
  assert.equal(JSON.parse(updatedPlugin).version, '1.2.0');
  const changelog = pr.updates.find(update => update.path === 'plugins/a/CHANGELOG.md').updater.updateContent('');
  assert.match(changelog, /marketplace-v1\.0\.0\.\.\.marketplace-v1\.1\.0/);
  assert.doesNotMatch(changelog, /a-v1\./);
  const merged = {number: 42, title: pr.title.toString(), body: pr.body.toString(),
    headBranchName: pr.headRefName.toString(), baseBranchName: 'main', sha: 'merged-sha', labels: ['autorelease: pending'], files};
  const releasesOut = [];
  for (const strategy of Object.values(strategies)) releasesOut.push(...await strategy.buildReleases(merged));
  assert.deepEqual(releasesOut.map(release => release.tag.toString()), ['marketplace-v1.1.0']);
  assert.match(releasesOut[0].notes, /a 1\.2\.0/);
  assert.match(releasesOut[0].notes, /new feature/);
  assert.doesNotMatch(releasesOut[0].notes, /b 1\.0\.0/);
  assert(releasesOut.every(release => release.sha === 'merged-sha'));
});
