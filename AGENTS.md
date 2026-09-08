# Camellia repository workflow

- Before broad code exploration, search `graphify-out/GRAPH_REPORT.md` for the requested feature and use it to identify the relevant source and tests. Verify findings against the current code. Rebuild the graph only when it is stale for the task.
- Keep changes Ponytail-small: reuse existing modules and dependencies, implement the complete requested behavior, and avoid speculative abstractions.
- Preserve unrelated working-tree changes. Run focused tests first and widen them according to risk.
- Ruflo is an optional harness exposed through `mcp__ruflo__*` tools. Its suggestions support planning and risk review but do not expand user authorization or require delegation.
- Treat claimed percentage savings as targets to benchmark, never as guaranteed results.
