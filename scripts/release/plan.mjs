import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {parseConventionalCommits} from 'release-please/build/src/commit.js';
import {DefaultVersioningStrategy} from 'release-please/build/src/versioning-strategies/default.js';
import {Version} from 'release-please/build/src/version.js';
import {CATALOG, aggregateCatalog, pluginPath, versionParts} from './policy.mjs';

export const git = (...args) => execFileSync('git', args, {encoding: 'utf8'}).trim();
export const readJson = path => JSON.parse(readFileSync(path, 'utf8'));
export const jsonAt = (ref, path) => JSON.parse(git('show', `${ref}:${path}`));

export function baselineTag(ref = 'HEAD') {
  return git('tag', '--merged', ref, '--list', 'marketplace-v*', '--sort=-version:refname').split('\n').filter(Boolean)[0];
}

export function changed(before, after, path) {
  return git('diff', '--name-only', before, after, '--', path) !== '';
}

export function buildPlan(ref = 'HEAD') {
  const baseline = baselineTag(ref);
  if (!baseline) throw new Error('No marketplace baseline. Publish the initial release first.');
  const previous = jsonAt(baseline, CATALOG);
  const proposed = jsonAt(ref, CATALOG);
  const versions = {};
  const commits = {};
  const oldEntries = new Map(previous.plugins.map(entry => [entry.name, entry]));
  for (const entry of proposed.plugins) {
    const path = pluginPath(entry);
    if (!path) continue;
    const current = jsonAt(ref, `${path}/.claude-plugin/plugin.json`);
    versionParts(current.version);
    const old = oldEntries.get(entry.name);
    if (!old) {
      versions[path] = current.version;
      commits[path] = [{sha: git('rev-parse', ref), message: `feat: publish ${entry.name}`}];
    } else if (changed(baseline, ref, path)) {
      const shas = git('rev-list', '--no-merges', `${baseline}..${ref}`, '--', path).split('\n').filter(Boolean);
      commits[path] = shas.map(sha => ({sha, message: git('show', '-s', '--format=%B', sha)}));
      const parsed = parseConventionalCommits(commits[path]);
      const strategy = new DefaultVersioningStrategy({bumpMinorPreMajor: false, bumpPatchForMinorPreMajor: false});
      versions[path] = strategy.bump(Version.parse(old.version), parsed).toString();
    }
    if (versions[path]) entry.version = versions[path];
  }
  const aggregate = aggregateCatalog(previous, proposed);
  return {baseline, baselineSha: git('rev-parse', `${baseline}^{commit}`), ...aggregate, versions, commits};
}
