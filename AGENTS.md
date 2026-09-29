## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read this checkout's graphify-out/GRAPH_REPORT.md for god nodes and community structure. If it is absent in a new worktree, run `graphify update .` here to generate it when available; otherwise state the gap. Do not substitute a report from another worktree.
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)

Worktree and tooling rules:
- Before editing, identify this checkout with `git rev-parse --show-toplevel`, `git rev-parse --git-common-dir`, `git rev-parse HEAD`, the checked-out branch or detached HEAD, and `git status --short`. A directory, Git branch, and remote PR ref are different identities.
- Keep concurrent writers in separate checkouts and on separate branches or detached HEADs; do not run builds, hook fixers, or file-changing checks in another writer's worktree.
- Run project tasks against the intended checkout with `mise -C <checkout> run <task>`. Inspect its exact mise config and `mise trust --show` before trusting a changed task definition; a shared trust result does not verify identical branch content.
- Inspect existing Git hook configuration before installing hk. Do not force a repository-local installation over an existing global hk installation, and treat an hk plan as selection evidence rather than proof that hooks executed.

See [worktree research](docs/research/codex-git-worktrees-mise-hk-20260929.md) for the current primary-source basis and local observations.
