import assert from 'node:assert/strict';

export const CATALOG = '.claude-plugin/marketplace.json';
export const VERSION_MANIFEST = '.release-please-manifest.json';
export const LEVELS = {none: 0, patch: 1, minor: 2, major: 3};

export function versionParts(version) {
  assert.match(version, /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/, `Unsupported version: ${version}`);
  return version.split('.').map(Number);
}

export function versionLevel(before, after) {
  const a = versionParts(before), b = versionParts(after);
  const index = b.findIndex((value, i) => value !== a[i]);
  if (index === -1) return 'none';
  assert(b[index] > a[index], `Version decreased: ${before} → ${after}`);
  return ['major', 'minor', 'patch'][index];
}

export function bumpVersion(version, level) {
  const [major, minor, patch] = versionParts(version);
  return {none: version, patch: `${major}.${minor}.${patch + 1}`,
    minor: `${major}.${minor + 1}.0`, major: `${major + 1}.0.0`}[level];
}

export function pluginPath(entry) {
  if (typeof entry.source !== 'string') return undefined;
  assert.match(entry.source, /^\.\/plugins\/[a-zA-Z0-9_-]+$/, `Unsafe plugin path: ${entry.source}`);
  return entry.source.slice(2);
}

export function aggregateCatalog(previous, proposed) {
  const next = structuredClone(proposed);
  const before = new Map(previous.plugins.map(p => [p.name, p]));
  const after = new Map(next.plugins.map(p => [p.name, p]));
  let level = 'none';
  const changes = [];
  const raise = value => { if (LEVELS[value] > LEVELS[level]) level = value; };
  for (const [name, entry] of before) {
    if (!after.has(name)) { raise('major'); changes.push(`Removed ${name}`); }
  }
  for (const [name, entry] of after) {
    const old = before.get(name);
    if (!old) { raise('minor'); changes.push(`Added ${name}${entry.version ? ` ${entry.version}` : ''}`); continue; }
    if (old.version !== entry.version) {
      assert(old.version && entry.version, `Explicit version required for ${name}`);
      raise(versionLevel(old.version, entry.version));
      changes.push(`${name}: ${old.version} → ${entry.version}`);
    }
  }
  // Version fields are generated, never treated as independent catalog changes.
  const normalize = catalog => {
    const copy = structuredClone(catalog);
    delete copy.version;
    if (copy.metadata) delete copy.metadata.version;
    copy.plugins = copy.plugins.map(({version, ...entry}) => entry).sort((a, b) => a.name.localeCompare(b.name));
    return JSON.stringify(copy);
  };
  if (normalize(previous) !== normalize(next)) {
    raise('patch');
    if (changes.length === 0) changes.push('Updated marketplace metadata or plugin sources');
  }
  next.metadata ??= {};
  next.metadata.version = bumpVersion(previous.metadata.version, level);
  return {level, catalog: next, changes};
}

export const BASELINE_NOTES = 'Initial 1.0.0 baseline. All plugin versions were deliberately reset to 1.0.0.';

export function pluginInventory(catalog) {
  return {marketplace: {name: catalog.name, version: catalog.metadata.version},
    plugins: catalog.plugins.map(({name, version, source}) => ({name, version, source}))};
}

export function marketplaceNotes(plan, componentNotes) {
  const blocks = [`## ${plan.catalog.metadata.version}`];
  for (const change of plan.changes) blocks.push(`- ${change}`);
  for (const entry of plan.catalog.plugins) {
    const notes = componentNotes[pluginPath(entry)];
    if (!notes) continue;
    const details = notes.replace(/^#{1,2} [^\n]+\n/, '').trim();
    blocks.push(`### ${entry.name} ${entry.version}\n\n${details.replace(/^### /gm, '#### ')}`);
  }
  return blocks.join('\n\n');
}
