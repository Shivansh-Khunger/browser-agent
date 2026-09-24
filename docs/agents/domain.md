# Domain docs

This repository uses a single-context domain-documentation layout.

## Before exploring

Read these resources when they exist:

- `CONTEXT.md` at the repository root.
- Relevant ADRs under `docs/adr/`.

If either resource does not exist, proceed silently. Do not create it pre-emptively. The domain-modeling workflow creates domain files lazily when terminology or durable architectural decisions are resolved.

## Layout

```text
/
├── CONTEXT.md
├── docs/
│   └── adr/
│       ├── 0001-example-decision.md
│       └── ...
├── browser_agent/
└── tests/
```

`CONTEXT.md` is a glossary, not an implementation specification. Use its canonical terms in issue titles, proposals, tests, and documentation. When a needed term is missing, reconsider whether the codebase already uses another term or raise the gap through domain modeling.

ADRs record decisions that are hard to reverse, surprising without context, and produced by a real trade-off. If proposed work conflicts with an ADR, surface the conflict rather than silently overriding it.
