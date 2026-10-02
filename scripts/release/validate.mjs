import assert from 'node:assert/strict';
import {existsSync, readdirSync, readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {CATALOG, VERSION_MANIFEST, pluginPath, versionParts} from './policy.mjs';
import {baselineTag, buildPlan, git, jsonAt, readJson} from './plan.mjs';

const catalog = readJson(CATALOG);
versionParts(catalog.metadata.version);
const registered = new Map();
for (const entry of catalog.plugins) {
  assert(!registered.has(entry.name), `Duplicate plugin ${entry.name}`);
  registered.set(entry.name, entry);
  const path = pluginPath(entry);
  if (path) {
    const manifest = readJson(`${path}/.claude-plugin/plugin.json`);
    assert.equal(entry.name, manifest.name, `Marketplace name mismatch: ${path}`);
    assert.equal(entry.version, manifest.version, `Marketplace version mismatch: ${path}`);
  }
}
for (const dir of readdirSync('plugins')) {
  const path = `plugins/${dir}`;
  if (!existsSync(`${path}/.claude-plugin/plugin.json`)) continue;
  execFileSync('bash', ['scripts/validate-plugin.sh', 'plugin', path], {stdio: 'inherit'});
  const claude = readJson(`${path}/.claude-plugin/plugin.json`);
  const codex = readJson(`${path}/.codex-plugin/plugin.json`);
  versionParts(claude.version);
  assert.equal(claude.name, codex.name, `Manifest name mismatch: ${path}`);
  assert.equal(claude.version, codex.version, `Manifest version mismatch: ${path}`);
}
const base = process.env.BASE_SHA;
if (base) {
  const shas = git('rev-list', '--no-merges', `${base}..HEAD`).split('\n').filter(Boolean);
  for (const sha of shas) {
    const message = git('show', '-s', '--format=%B', sha);
    assert.match(message.split('\n')[0], /^(feat|fix|docs|style|refactor|perf|test|ci|build|chore)(\([^\n)]+\))?!?: \S.*$/,
      `Non-conventional commit ${sha}`);
    for (const line of message.split('\n')) {
      if (/^BREAKING[ -]CHANGE:/.test(line)) assert.match(line, /^BREAKING[ -]CHANGE:\s+\S/, `Empty breaking change: ${sha}`);
    }
  }
  const previous = jsonAt(base, CATALOG);
  for (const file of git('diff', '--name-only', '--diff-filter=A', base, 'HEAD').split('\n')) {
    if (!/^plugins\/[^/]+\/\.claude-plugin\/plugin.json$/.test(file)) continue;
    const manifest = readJson(file);
    assert(registered.has(manifest.name), `New plugin ${manifest.name} is not registered`);
  }
  const baseline = baselineTag(base);
  const releasePr = process.env.RELEASE_PR === 'true';
  if (baseline && releasePr) {
    const plan = buildPlan(base);
    assert.notEqual(plan.level, 'none', 'Empty release PR');
    assert.deepEqual(catalog, plan.catalog, 'Release catalog differs from calculated plan');
    const expectedVersions = {'.': plan.catalog.metadata.version};
    const allowed = new Set([CATALOG, VERSION_MANIFEST, 'CHANGELOG.md']);
    for (const entry of plan.catalog.plugins) {
      const path = pluginPath(entry);
      if (!path) continue;
      expectedVersions[path] = entry.version;
      if (plan.versions[path]) {
        for (const file of ['.claude-plugin/plugin.json', '.codex-plugin/plugin.json', 'CHANGELOG.md']) allowed.add(`${path}/${file}`);
        for (const dir of ['.claude-plugin', '.codex-plugin']) {
          const file = `${path}/${dir}/plugin.json`;
          const expected = jsonAt(base, file);
          expected.version = entry.version;
          assert.deepEqual(readJson(file), expected, `Unexpected manifest edit: ${file}`);
        }
        assert(readFileSync(`${path}/CHANGELOG.md`, 'utf8').includes(entry.version), `Missing changelog for ${entry.name}`);
      }
    }
    assert.deepEqual(readJson(VERSION_MANIFEST), expectedVersions, 'Release manifest differs from plan');
    assert(readFileSync('CHANGELOG.md', 'utf8').includes(plan.catalog.metadata.version), 'Missing marketplace changelog');
    for (const file of git('diff', '--name-only', base, 'HEAD').split('\n').filter(Boolean)) {
      assert(allowed.has(file), `Unexpected file in release PR: ${file}`);
    }
  } else if (baseline) {
    assert.equal(catalog.metadata.version, previous.metadata.version, 'Marketplace version is managed by release automation');
    assert.deepEqual(readJson(VERSION_MANIFEST), jsonAt(base, VERSION_MANIFEST), 'Release manifest is managed by automation');
    for (const entry of previous.plugins) {
      const path = pluginPath(entry);
      if (!path || !existsSync(`${path}/.claude-plugin/plugin.json`)) continue;
      assert.equal(readJson(`${path}/.claude-plugin/plugin.json`).version,
        jsonAt(base, `${path}/.claude-plugin/plugin.json`).version, 'Existing plugin versions are managed by automation');
    }
  }
  const oldNames = new Set(previous.plugins.map(entry => entry.name));
  const added = catalog.plugins.filter(entry => !oldNames.has(entry.name)).map(entry => entry.name);
  if (added.length) execFileSync('bash', ['scripts/validate-plugin.sh', 'check-sources', CATALOG, ...added], {stdio: 'inherit'});
}
console.log('Catalog, manifests, commits and release policy are valid.');
