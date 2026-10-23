# ChaosInject

AI-driven chaos engineering for a cloud-native app. Merged from two repos (see NOTICE):

- **Execution core** (from Shaurya-34/Hackathon): `sandbox/hotel` (DeathStarBench Hotel Reservation on Docker Compose), `chaos/` (controller, faults, safety rules, results), `experiments/`, `docs/`.
- **API, dashboard, intelligence** (from ssaaach/AI-Chaos-Pilot): `backend/` (FastAPI chaos API, the two agents, the score), `frontend/` (React + Vite), `contracts/`. The unused parts of that import were removed and remain in the git history.

Core rule: **the controller enforces safety, not the AI.** AI output is a proposal that the controller validates against `chaos/policy.json`; AI code never runs Docker or shell commands.

## Agent channel (read this first, every session)

Two humans work on this repo, each with their own Claude Code. The Claudes talk through `COMMUNICATION.md` on the **`comms` branch** (not on `main`, because `main` only accepts PRs).

One-time setup, from the repo root:

```bash
git fetch origin comms
git worktree add ../ChaosInject-comms comms
```

At the start of every session, and before starting any task:

```bash
git -C ../ChaosInject-comms pull --rebase
```

Then read `../ChaosInject-comms/COMMUNICATION.md` and follow its protocol. Tell your human about any new messages addressed to you.

## Ownership

| Owner | Paths |
|---|---|
| Shaurya-34 | `chaos/`, `sandbox/`, `experiments/`, API endpoints that wrap the controller, Prometheus exporters |
| Gauthamop | `backend/app/intelligence/`, `frontend/`, agents, PromQL/dashboard charts |
| Both (agree in the PR) | `contracts/`, `backend/app/main.py`, `CLAUDE.md`, `README.md` |

## Workflow

- Branch `<github-user>/<short-topic>` from an up-to-date `main`, open a PR, and have the other person review it. No direct pushes to `main`.
- Never commit secrets, `.env`, kubeconfigs, or `results/`.

## Commands

```bash
# Execution core (stdlib only)
python -m unittest tests.test_controller tests.test_hooks tests.test_hotel_setup
python -m chaos.controller experiments/hotel_geo_stop.json
python -m chaos.hooks list

# Backend and frontend (need the venv from `python scripts/dev.py setup`)
python -m pytest -q
npm run build --prefix frontend
```

On Windows use `python`; on macOS/Linux use `python3`.
