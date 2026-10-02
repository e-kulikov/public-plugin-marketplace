# e-kulikov Public Plugin Marketplace

Public plugins for Claude Code and GitHub Copilot CLI.

## Install

```bash
# Claude Code
claude plugin marketplace add 'e-kulikov/public-plugin-marketplace#stable'
claude plugin marketplace add vercel-labs/agent-browser
claude plugin install chatgpt-backup@e-kulikov-public-ai-marketplace

# GitHub Copilot CLI
copilot plugin marketplace add 'e-kulikov/public-plugin-marketplace#stable'
copilot plugin marketplace add vercel-labs/agent-browser
copilot plugin install chatgpt-backup@e-kulikov-public-ai-marketplace
```

See [chatgpt-backup](plugins/chatgpt-backup/README.md) for requirements and usage.

## Update

```bash
claude plugin marketplace update e-kulikov-public-ai-marketplace
copilot plugin marketplace update e-kulikov-public-ai-marketplace
```

## Development and releases

```bash
bash scripts/setup.sh
npm ci --ignore-scripts
npm run validate
npm test
bash plugins/chatgpt-backup/tests/run-tests.sh
```

Develop on branches using Conventional Commits and open pull requests to `main`.
Release automation owns versions and changelogs. The marketplace is released as
`marketplace-v<version>` with a `plugin-versions.json` inventory; plugins are versioned
components of that snapshot. Successful publication advances `stable`.

See [docs/releases.md](docs/releases.md) for the complete release policy and recovery.
