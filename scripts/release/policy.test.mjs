import test from 'node:test';
import assert from 'node:assert/strict';
import {aggregateCatalog, bumpVersion, versionLevel, pluginPath, pluginInventory, marketplaceNotes, BASELINE_NOTES} from './policy.mjs';

const catalog = () => ({name: 'test', metadata: {version: '1.0.0'}, plugins: [
  {name: 'a', source: './plugins/a', version: '1.0.0', description: 'A'},
  {name: 'b', source: './plugins/b', version: '1.0.0', description: 'B'},
]});

test('mixed release uses the highest level, without linking component versions', () => {
  for (const [versions, expected] of [
    [['1.0.1', '1.0.2'], '1.0.1'], [['1.0.1', '1.2.0'], '1.1.0'], [['2.0.0', '1.2.0'], '2.0.0'],
  ]) {
    const proposed = catalog();
    proposed.plugins.forEach((entry, i) => {entry.version = versions[i];});
    const result = aggregateCatalog(catalog(), proposed);
    assert.equal(result.catalog.metadata.version, expected);
    assert.deepEqual(result.catalog.plugins.map(p => p.version), versions);
  }
});

test('addition is minor; removal and rename are major', () => {
  const added = catalog();
  added.plugins.push({name: 'c', source: './plugins/c', version: '0.1.0'});
  assert.equal(aggregateCatalog(catalog(), added).level, 'minor');
  const removed = catalog(); removed.plugins.pop();
  assert.equal(aggregateCatalog(catalog(), removed).level, 'major');
  const renamed = catalog(); renamed.plugins[0].name = 'renamed';
  assert.equal(aggregateCatalog(catalog(), renamed).level, 'major');
});

test('metadata edits release patch; ordering and generated version alone do not release', () => {
  const metadata = catalog(); metadata.plugins[0].description = 'Updated';
  assert.equal(aggregateCatalog(catalog(), metadata).level, 'patch');
  const reordered = catalog(); reordered.plugins.reverse(); reordered.metadata.version = '5.0.0';
  assert.equal(aggregateCatalog(catalog(), reordered).level, 'none');
});

test('numeric versions, pre-1.0 and decreases are explicit', () => {
  assert.equal(versionLevel('0.9.0', '1.0.0'), 'major');
  assert.equal(versionLevel('1.9.9', '1.10.0'), 'minor');
  assert.equal(bumpVersion('0.2.0', 'major'), '1.0.0');
  assert.throws(() => versionLevel('2.0.0', '1.0.0'), /decreased/);
  assert.throws(() => versionLevel('1.0.0', '1.1.0-beta.1'), /Unsupported/);
  assert.throws(() => pluginPath({source: './plugins/../../secrets'}), /Unsafe/);
});

test('inventory includes every plugin while release notes include only changed plugins', () => {
  const proposed = catalog(); proposed.plugins[0].version = '1.0.1';
  const plan = aggregateCatalog(catalog(), proposed);
  const notes = marketplaceNotes(plan, {'plugins/a': '## 1.0.1\n\n### fix\n\n- Correct export'});
  assert.match(notes, /a 1\.0\.1/);
  assert.match(notes, /Correct export/);
  assert.doesNotMatch(notes, /b 1\.0\.0/);
  assert.deepEqual(pluginInventory(plan.catalog).plugins.map(p => p.version), ['1.0.1', '1.0.0']);
  assert.equal(BASELINE_NOTES, 'Initial 1.0.0 baseline. All plugin versions were deliberately reset to 1.0.0.');
});
