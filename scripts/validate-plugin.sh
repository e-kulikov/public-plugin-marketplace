#!/usr/bin/env bash
# Validate plugin directories and marketplace index.
# Usage:
#   ./scripts/validate-plugin.sh plugin <path>               — validate a plugin dir
#   ./scripts/validate-plugin.sh marketplace <path>          — validate marketplace.json (schema only)
#   ./scripts/validate-plugin.sh check-sources <path> [names...]  — check source reachability
#       names: optional list of plugin names to check (default: all non-relative entries)
#       Auth: GIT_AUTH_TOKEN env var for git sources; NPM_TOKEN for npm sources.
#
# Exit 0 on success. Non-zero on any failure.

set -euo pipefail

MODE="${1:-}"
TARGET="${2:-}"

err() { echo "ERROR: $*" >&2; }
warn() { echo "WARNING: $*" >&2; }
ok() { echo "OK: $*"; }

validate_plugin() {
  local dir="$1"

  if [ ! -d "$dir" ]; then
    err "Plugin directory does not exist: $dir"
    return 1
  fi

  local manifest="$dir/.claude-plugin/plugin.json"
  if [ ! -f "$manifest" ]; then
    err "Missing manifest: $manifest"
    return 1
  fi

  # JSON syntax
  if ! jq . "$manifest" > /dev/null 2>&1; then
    err "Invalid JSON: $manifest"
    return 1
  fi

  # Required fields
  local name version description
  name=$(jq -r '.name // empty' "$manifest")
  version=$(jq -r '.version // empty' "$manifest")
  description=$(jq -r '.description // empty' "$manifest")

  [ -n "$name" ]        || { err "Missing .name in $manifest"; return 1; }
  [ -n "$version" ]     || { err "Missing .version in $manifest"; return 1; }
  [ -n "$description" ] || { err "Missing .description in $manifest"; return 1; }

  # Component directories exist if declared
  local skills agents hooks monitors mcp lsp
  skills=$(jq -r '.skills // empty' "$manifest")
  agents=$(jq -r '.agents // empty' "$manifest")
  hooks=$(jq -r '.hooks // empty' "$manifest")
  monitors=$(jq -r '.monitors // empty' "$manifest")
  mcp=$(jq -r '.mcpServers // empty' "$manifest")
  lsp=$(jq -r '.lspServers // empty' "$manifest")

  check_path() {
    local field="$1" rel="$2"
    [ -z "$rel" ] && return 0
    local abs="$dir/${rel#./}"
    [ -e "$abs" ] || { err "Declared $field path does not exist: $abs"; return 1; }
  }

  check_path skills "$skills"
  check_path agents "$agents"
  check_path hooks "$hooks"
  check_path monitors "$monitors"
  check_path mcpServers "$mcp"
  check_path lspServers "$lsp"

  # hooks/hooks.json is auto-loaded by the runtime; re-declaring it in
  # manifest.hooks causes a "duplicate hooks file" error on plugin load.
  if [ -n "$hooks" ]; then
    local hooks_resolved
    hooks_resolved="$(cd "$dir" && realpath "${hooks#./}" 2>/dev/null || true)"
    local auto_hooks="$dir/hooks/hooks.json"
    if [ -f "$auto_hooks" ] && [ "$hooks_resolved" = "$(realpath "$auto_hooks")" ]; then
      err "manifest.hooks points to hooks/hooks.json, which is auto-loaded by the runtime. Remove the hooks field from plugin.json to avoid a duplicate-hooks error."
      return 1
    fi
  fi

  ok "Plugin valid: $name@$version ($dir)"
}

validate_marketplace() {
  local manifest="$1"

  if [ ! -f "$manifest" ]; then
    err "File not found: $manifest"
    return 1
  fi

  # JSON syntax
  if ! jq . "$manifest" > /dev/null 2>&1; then
    err "Invalid JSON: $manifest"
    return 1
  fi

  # Required top-level fields
  local name
  name=$(jq -r '.name // empty' "$manifest")
  [ -n "$name" ] || { err "Missing .name in $manifest"; return 1; }

  # Guard against missing .plugins key
  if ! jq -e '.plugins | type == "array"' "$manifest" > /dev/null 2>&1; then
    err "Missing or invalid .plugins array in $manifest"
    return 1
  fi

  # Required fields per plugin entry
  local bad_entries
  bad_entries=$(jq -r '.plugins[] | select(.name == null or .name == "" or .source == null or .source == "" or .description == null or .description == "") | .name // "<unnamed>"' "$manifest")
  if [ -n "$bad_entries" ]; then
    err "Plugin entries missing required fields (name, source, description): $bad_entries"
    return 1
  fi

  # Duplicate names
  local dupes
  dupes=$(jq -r '.plugins[].name' "$manifest" | sort | uniq -d)
  if [ -n "$dupes" ]; then
    err "Duplicate plugin names in $manifest: $dupes"
    return 1
  fi

  local count
  count=$(jq '.plugins | length' "$manifest")
  ok "Marketplace valid: $name ($count plugins)"
}

# Check that non-relative plugin sources actually resolve.
# Requires GIT_AUTH_TOKEN for private git sources, NPM_TOKEN for npm sources.
check_sources() {
  local manifest="$1"
  shift
  local filter_names=("$@")  # optional: only check these plugin names

  if [ ! -f "$manifest" ]; then
    err "File not found: $manifest"
    return 1
  fi

  # Guard against missing .plugins key
  if ! jq -e '.plugins | type == "array"' "$manifest" > /dev/null 2>&1; then
    err "Missing or invalid .plugins array in $manifest"
    return 1
  fi

  # Set up temporary config files for git and npm auth (scoped to this process only)
  local _tmpgitconfig _tmpnpmrc
  _tmpgitconfig=$(mktemp)
  _tmpnpmrc=$(mktemp)
  trap 'rm -f "$_tmpgitconfig" "$_tmpnpmrc"' RETURN

  # Configure git auth if token provided (in temp config, not global)
  if [ -n "${GIT_AUTH_TOKEN:-}" ]; then
    GIT_CONFIG_GLOBAL="$_tmpgitconfig" git config --global \
      url."https://oauth2:${GIT_AUTH_TOKEN}@".insteadOf "https://"
  fi

  local failed=0

  # Extract non-relative entries as JSON lines: {"name":"...","source":{...}}
  local jq_filter
  if [ ${#filter_names[@]} -gt 0 ]; then
    # Build jq select expression for specific names
    local name_list
    name_list=$(printf '"%s",' "${filter_names[@]}")
    name_list="[${name_list%,}]"
    jq_filter=".plugins[] | select(.name as \$n | ${name_list} | index(\$n) != null)"
  else
    jq_filter='.plugins[]'
  fi

  while IFS= read -r entry; do
    local plugin_name src_type
    plugin_name=$(echo "$entry" | jq -r '.name')
    src_type=$(echo "$entry" | jq -r '
      if (.source | type) == "string" then
        if (.source | startswith("./")) then "relative" else "unknown" end
      else .source.source end
    ')

    case "$src_type" in
      relative)
        # Local path — skip reachability check
        ok "Source reachable (local): $plugin_name"
        ;;
      github)
        local repo
        repo=$(echo "$entry" | jq -r '.source.repo')
        local url="https://github.com/${repo}"
        if GIT_CONFIG_GLOBAL="$_tmpgitconfig" git ls-remote "$url" HEAD > /dev/null 2>&1; then
          ok "Source reachable (github): $plugin_name ($repo)"
        else
          err "Source not reachable (github): $plugin_name ($repo)"
          failed=1
        fi
        ;;
      url|git-subdir)
        local url
        url=$(echo "$entry" | jq -r '.source.url')
        if GIT_CONFIG_GLOBAL="$_tmpgitconfig" git ls-remote "$url" HEAD > /dev/null 2>&1; then
          ok "Source reachable (git url): $plugin_name ($url)"
        else
          err "Source not reachable (git url): $plugin_name ($url)"
          failed=1
        fi
        ;;
      npm)
        local pkg
        pkg=$(echo "$entry" | jq -r '.source.package')
        local registry
        registry=$(echo "$entry" | jq -r '.source.registry // "https://registry.npmjs.org"')
        # Set up npm auth in temp npmrc file if token provided
        if [ -n "${NPM_TOKEN:-}" ]; then
          local host
          host=$(echo "$registry" | sed 's|https://||')
          echo "//${host}/:_authToken=${NPM_TOKEN}" >> "$_tmpnpmrc"
        fi
        if npm view "$pkg" version --registry "$registry" --userconfig "$_tmpnpmrc" > /dev/null 2>&1; then
          ok "Source reachable (npm): $plugin_name ($pkg)"
        else
          err "Source not reachable (npm): $plugin_name ($pkg)"
          failed=1
        fi
        ;;
      *)
        err "Unknown source type '$src_type' for $plugin_name — cannot verify reachability"
        failed=1
        ;;
    esac
  done < <(jq -c "$jq_filter" "$manifest")

  return $failed
}

case "$MODE" in
  plugin)
    [ -n "$TARGET" ] || { err "Usage: $0 plugin <dir>"; exit 1; }
    validate_plugin "$TARGET"
    ;;
  marketplace)
    [ -n "$TARGET" ] || { err "Usage: $0 marketplace <file>"; exit 1; }
    validate_marketplace "$TARGET"
    ;;
  check-sources)
    [ -n "$TARGET" ] || { err "Usage: $0 check-sources <file> [name...]"; exit 1; }
    shift 2
    check_sources "$TARGET" "$@"
    ;;
  *)
    err "Usage: $0 plugin <dir> | marketplace <file> | check-sources <file> [name...]"
    exit 1
    ;;
esac
