import {registerPlugin, registerReleaseType} from 'release-please';
import {BaseStrategy} from 'release-please/build/src/strategies/base.js';
import {ManifestPlugin} from 'release-please/build/src/plugin.js';
import {Changelog} from 'release-please/build/src/updaters/changelog.js';
import {GenericJson} from 'release-please/build/src/updaters/generic-json.js';
import {Version} from 'release-please/build/src/version.js';
import {buildStrategy} from 'release-please/build/src/factory.js';
import {TagName} from 'release-please/build/src/util/tag-name.js';
import {CATALOG, VERSION_MANIFEST, pluginPath, marketplaceNotes} from './policy.mjs';

export const sections = ['feat', 'fix', 'perf', 'docs', 'refactor', 'style', 'test', 'build', 'ci', 'chore']
  .map(type => ({type, section: type, hidden: false}));

export function registerAdapter(plan) {
  class JsonPluginStrategy extends BaseStrategy {
    async buildReleaseNotes(commits, version, _tag, _latest, rawCommits) {
      const tag = new TagName(Version.parse(plan.catalog.metadata.version), 'marketplace', '-');
      const latest = {tag: TagName.parse(plan.baseline), sha: plan.baselineSha};
      return super.buildReleaseNotes(commits, version, tag, latest, rawCommits);
    }
    async buildUpdates({newVersion, changelogEntry}) {
      return [
        {path: `${this.path}/CHANGELOG.md`, createIfMissing: true,
          updater: new Changelog({version: newVersion, changelogEntry})},
        ...['.claude-plugin', '.codex-plugin'].map(dir => ({
          path: `${this.path}/${dir}/plugin.json`, createIfMissing: false,
          updater: new GenericJson('$.version', newVersion),
        })),
      ];
    }
  }
  class MarketplaceStrategy extends BaseStrategy {
    async buildUpdates({newVersion}) {
      const entry = `## ${newVersion}\n\n${plan.changes.map(change => `- ${change}`).join('\n')}`;
      return [
        {path: CATALOG, createIfMissing: false,
          updater: {updateContent: () => `${JSON.stringify(plan.catalog, null, 2)}\n`}},
        {path: 'CHANGELOG.md', createIfMissing: true,
          updater: new Changelog({version: newVersion, changelogEntry: entry})},
      ];
    }
    async buildReleaseNotes() {
      return `## ${plan.catalog.metadata.version}\n\n${plan.changes.map(change => `- ${change}`).join('\n')}`;
    }
  }
  class MarketplacePlugin extends ManifestPlugin {
    async preconfigure(strategies, commitsByPath) {
      // Feed deterministic candidates into release-please's PR and release lifecycle.
      // Unchanged packages must not release because another component's tag is older.
      for (const path of Object.keys(strategies)) {
        if (path === '.') {
          commitsByPath[path] = plan.level === 'none' ? [] : [{sha: plan.baselineSha,
            message: 'chore: release marketplace'}];
        } else {
          commitsByPath[path] = plan.versions[path] ? plan.commits[path] : [];
        }
        const releaseAs = path === '.' ? plan.catalog.metadata.version : plan.versions[path];
        if (releaseAs) strategies[path] = await buildStrategy({...this.repositoryConfig[path],
          github: this.github, path, targetBranch: this.targetBranch, releaseAs, logger: this.logger});
      }
      return strategies;
    }
    async run(candidates) {
      const root = candidates.find(candidate => candidate.path === '.');
      if (!root) return candidates;
      const componentNotes = {};
      for (const candidate of candidates.filter(candidate => candidate.path !== '.')) {
        componentNotes[candidate.path] = candidate.pullRequest.body.releaseData[0].notes;
        // Plugins are constituent artifacts, not independently published releases.
        candidate.pullRequest.body.releaseData = [];
      }
      const notes = marketplaceNotes(plan, componentNotes);
      root.pullRequest.body.releaseData[0].notes = notes;
      root.pullRequest.updates.find(update => update.path === 'CHANGELOG.md').updater =
        new Changelog({version: root.pullRequest.version, changelogEntry: notes});
      for (const candidate of candidates) {
        candidate.pullRequest.updates = candidate.pullRequest.updates.filter(update => update.path !== VERSION_MANIFEST);
      }
      const versions = {'.': plan.catalog.metadata.version};
      for (const entry of plan.catalog.plugins) {
        const path = pluginPath(entry);
        if (path) versions[path] = entry.version;
      }
      root.pullRequest.updates.push({path: VERSION_MANIFEST, createIfMissing: false,
        updater: {updateContent: () => `${JSON.stringify(versions, null, 2)}\n`}});
      return candidates;
    }
  }
  registerReleaseType('plugin-json', options => new JsonPluginStrategy(options));
  registerReleaseType('marketplace-json', options => new MarketplaceStrategy(options));
  registerPlugin('marketplace-catalog', options => new MarketplacePlugin(options.github, options.targetBranch, options.repositoryConfig, options.logger));
}

export function manifestConfiguration(catalog) {
  const config = {'.': {releaseType: 'marketplace-json', component: 'marketplace', packageName: 'marketplace',
    includeComponentInTag: true, tagSeparator: '-', includeVInTag: true, changelogSections: sections}};
  for (const entry of catalog.plugins) {
    if (typeof entry.source !== 'string') continue;
    config[entry.source.slice(2)] = {releaseType: 'plugin-json', component: entry.name, packageName: entry.name,
      skipGithubRelease: true,
      initialVersion: entry.version, includeComponentInTag: true, tagSeparator: '-', includeVInTag: true,
      changelogSections: sections, bumpMinorPreMajor: false, bumpPatchForMinorPreMajor: false};
  }
  return config;
}

export function parsedVersions(versions) {
  return Object.fromEntries(Object.entries(versions).map(([path, version]) => [path, Version.parse(version)]));
}
