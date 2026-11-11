# Home-search

Scheduled Houston home search for a 2027 purchase ($800k–$1.2M) in Bellaire and
Spring Branch / Memorial, gated on school zoning, with a rent-vs-buy verdict and
email digests. Current state: scaffold. Stubs raise `NotImplementedError("WPn")`.

## How work is done here

- The orchestrating session plans, reviews and integrates. Implementation is
  delegated one work package at a time to the `implementer` agent
  (`.claude/agents/implementer.md`, runs on Opus).
- `docs/TASKS.md` is the backlog and the source of truth for scope and "done".
  Tick the box in the same commit that completes a package.
- Specs: `docs/ARCHITECTURE.md` (flow, scoring, privacy), `docs/FINANCE_MODEL.md` (formulas).

## Commands

```
pip install -e ".[dev]"
pytest                      # network tests are excluded by default
pytest -m network           # live-site tests, run by hand only
ruff check .
home-search doctor          # validates config, shows what is set up
home-search scan --dry-run
```

## Rules

- **This repo is public.** Never commit `config/profile.yaml`, `.env`, anything
  under `out/`, real email addresses, or unscrubbed `.eml` fixtures. Never print
  profile values. See "Privacy rules" in `docs/ARCHITECTURE.md`.
- Stub signatures and `models.py` are the contract between packages. Propose a
  change to the orchestrator instead of editing them inside a package.
- No number in the finance code: every rate, limit and percentage lives in
  `config/assumptions.yaml`.
- Listing sites: one request per zip per run, jittered pauses, no retry on
  403/429, no detail-page crawling, no evasion (proxies, header spoofing,
  CAPTCHA workarounds). A blocked source raises `SourceUnavailable`.
- Zoning is `VERIFIED` only from a boundary polygon. Never infer it from a zip.
- Scheduled workflows stay disabled until WP11.
