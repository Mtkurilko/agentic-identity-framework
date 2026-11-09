# Changelog

## 0.2.0 (2026-11)

- **Persona.** `persona.md` describes how you want every AI to behave with you. Facts in the
  `persona` topic add to it, so models can record "stop apologizing" for the next model to see.
  Both render in their own capsule section. Edit with `aif edit persona`.
- **Onboarding.** `aif init` asks five optional questions on first run. While a store is nearly
  empty, the capsule asks models to learn the basics. A new MCP prompt, `get_to_know_me`, runs a
  short interview.
- **Hooks.** `aif hooks install` adds Claude Code hooks. They point the model at people on file when
  you mention them, and remind it once when you shared something durable that never got saved.
- **MCP 2026-07-28.** The server is dual-era: legacy `initialize` clients and stateless clients
  (`server/discover`, per-request `_meta`) both work, over stdio and HTTP. List results carry
  `ttlMs` and `cacheScope: private`.
- Tool titles and annotations. The `remember` description now spells out when to save.
- `aif setup` prints a rules block for `AGENTS.md`, `.cursorrules` and similar files.
- MCP over HTTP (`aif serve --http`) with bearer-token auth, so many devices can share one store.
- `merge` for duplicate people and things.

## 0.1.0 (2026-09)

- First release: the `aif/1` format, CLI, MCP server over stdio, capsule, people and entities,
  beliefs, and git sync.
