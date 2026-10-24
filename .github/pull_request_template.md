## What this changes

## Why

## How it was tested
<!-- Unit tests alone are not enough for pipeline changes: say what you ran end to end
     (for example a bad deploy against real Postgres and Redis) and what you observed. -->
- [ ] `pytest server/tests sdk/tests`
- [ ] `ruff check .`
- [ ] Dashboard: `npm run lint && npm run build` (if `dashboard/` changed)
- [ ] Migrations apply to an empty database and are safe to re-run (if SQL changed)

## Checklist
- [ ] Read-only toward customer code; nothing writes to repositories
- [ ] No secrets, stack traces or diffs in logs; sensitive data stored encrypted
- [ ] `.env.example`, README and `DECISIONS.md` updated if behaviour or settings changed
