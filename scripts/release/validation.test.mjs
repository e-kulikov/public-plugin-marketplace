import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync, writeFileSync, mkdirSync, rmSync, copyFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {git, buildPlan} from './plan.mjs';

test('ordinary PR needs no bump; release PR must exactly match the computed snapshot', () => {
  const root = process.cwd(), dir = mkdtempSync(join(tmpdir(), 'marketplace-validation-test-'));
  try {
    process.chdir(dir);
    git('init', '-q'); git('config', 'user.email', 'test@example.com'); git('config', 'user.name', 'Test');
    mkdirSync('.claude-plugin'); mkdirSync('scripts');
    copyFileSync(join(root, 'scripts/validate-plugin.sh'), 'scripts/validate-plugin.sh');
    for (const client of ['.claude-plugin', '.codex-plugin']) mkdirSync(`plugins/a/${client}`, {recursive: true});
    const save = (file, value) => writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
    const plugin = {name: 'a', description: 'Test plugin', version: '1.0.0'};
    save('plugins/a/.claude-plugin/plugin.json', plugin); save('plugins/a/.codex-plugin/plugin.json', plugin);
    save('.claude-plugin/marketplace.json', {name: 'test', metadata: {version: '1.0.0'},
      plugins: [{...plugin, source: './plugins/a'}]});
    save('.release-please-manifest.json', {'.': '1.0.0', 'plugins/a': '1.0.0'});
    const commit = message => {git('add', '.'); git('commit', '-qm', message);};
    commit('chore: baseline'); const baseline = git('rev-parse', 'HEAD'); git('tag', 'marketplace-v1.0.0');
    writeFileSync('plugins/a/README.md', 'Updated docs\n'); commit('docs: explain a');
    const base = git('rev-parse', 'HEAD');
    const validate = (baseSha, release) => execFileSync('node', [join(root, 'scripts/release/validate.mjs')], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
      env: {...process.env, BASE_SHA: baseSha, RELEASE_PR: String(release)},
    });
    assert.match(validate(baseline, false), /are valid/);
    const plan = buildPlan(); assert.equal(plan.level, 'patch');
    save('.claude-plugin/marketplace.json', plan.catalog);
    plugin.version = '1.0.1';
    save('plugins/a/.claude-plugin/plugin.json', plugin); save('plugins/a/.codex-plugin/plugin.json', plugin);
    save('.release-please-manifest.json', {'.': '1.0.1', 'plugins/a': '1.0.1'});
    writeFileSync('CHANGELOG.md', '## 1.0.1\n'); writeFileSync('plugins/a/CHANGELOG.md', '## 1.0.1\n');
    commit('chore: release main');
    assert.match(validate(base, true), /are valid/);
    assert.throws(() => validate(base, false), /managed by release automation/);
    const unexpected = {...plan.catalog, owner: {name: 'Unexpected edit'}};
    save('.claude-plugin/marketplace.json', unexpected); commit('chore: tamper with catalog');
    assert.throws(() => validate(base, true), /differs from calculated plan/);
  } finally {
    process.chdir(root); rmSync(dir, {recursive: true, force: true});
  }
});
