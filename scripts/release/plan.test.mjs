import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, writeFileSync, mkdirSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {buildPlan, git} from './plan.mjs';

test('git-based planning: infrastructure ignored, docs patch, feat minor, breaking major, additions preserved', () => {
  const original = process.cwd(), dir = mkdtempSync(join(tmpdir(), 'marketplace-release-test-'));
  try {
    process.chdir(dir);
    git('init', '-q'); git('config', 'user.email', 'test@example.com'); git('config', 'user.name', 'Test');
    mkdirSync('.claude-plugin'); mkdirSync('plugins/a/.claude-plugin', {recursive: true});
    const catalog = {name: 'test', metadata: {version: '1.0.0'}, plugins: [{name: 'a', source: './plugins/a', version: '1.0.0'}]};
    writeFileSync('.claude-plugin/marketplace.json', JSON.stringify(catalog));
    writeFileSync('plugins/a/.claude-plugin/plugin.json', '{"name":"a","version":"1.0.0"}');
    const commit = message => {git('add', '.'); git('commit', '-qm', message);};
    commit('chore: baseline'); git('tag', 'marketplace-v1.0.0');
    writeFileSync('ci.txt', 'internal'); commit('ci: adjust workflow');
    assert.equal(buildPlan().level, 'none');
    writeFileSync('plugins/a/README.md', 'docs'); commit('docs: explain plugin');
    assert.equal(buildPlan().versions['plugins/a'], '1.0.1');
    writeFileSync('plugins/a/skill.md', 'feature'); commit('feat: new skill');
    assert.equal(buildPlan().versions['plugins/a'], '1.1.0');
    assert.equal(buildPlan().catalog.metadata.version, '1.1.0');
    writeFileSync('plugins/a/skill.md', 'breaking'); commit('fix: change interface\n\nBREAKING CHANGE: remove old argument');
    assert.equal(buildPlan().versions['plugins/a'], '2.0.0');
    assert.equal(buildPlan().catalog.metadata.version, '2.0.0');
    mkdirSync('plugins/b/.claude-plugin', {recursive: true});
    writeFileSync('plugins/b/.claude-plugin/plugin.json', '{"name":"b","version":"0.3.0"}');
    catalog.plugins.push({name: 'b', source: './plugins/b', version: '0.3.0'});
    writeFileSync('.claude-plugin/marketplace.json', JSON.stringify(catalog)); commit('feat: register b');
    assert.equal(buildPlan().versions['plugins/b'], '0.3.0');
  } finally {
    process.chdir(original); rmSync(dir, {recursive: true, force: true});
  }
});
