# AI-Driven Chaos Engineering

An AI-assisted chaos-engineering system that injects **controlled failures** into a sandboxed cloud-native app, measures the impact, stops automatically when safety thresholds are breached, and explains weaknesses and fixes.

## Questions it answers

- What happens when a service or dependency fails?
- How much do latency, errors and health degrade?
- Does the app recover, and how fast?
- When should an experiment be stopped?
- Which weaknesses should be fixed first?

## Architecture

```text
Sample app (Docker Compose)
        │
Dependency mapper ──► Experiment controller ──► Chaos tool (fault injection)
                              │
                     Metrics / health monitor
                              │
                     Safety controller (auto-stop, kill switch, timeout)
                              │
                     Results store ──► AI planner & analysis (Day 2)
```

The controller enforces safety, not the AI. AI output is a recommendation, never a command.

## Safety rules

- Runs only in our own local sandbox; never production, shared infra or other teams' containers.
- Allowlisted containers and experiment types only.
- Thresholds are set before each run, with a hard timeout and a manual kill switch.
- The app must be confirmed healthy after every experiment.

## Status

- **Sample app:** [DeathStarBench Hotel Reservation](https://github.com/delimitrou/DeathStarBench/tree/master/hotelReservation) (Apache-2.0). Go gRPC microservices with Consul, memcached, MongoDB, Jaeger tracing and a wrk2 load generator, all on Docker Compose.
- **Chaos tool:** evaluating Pumba / ChaosLabs
- **Progress:** tracked on the [project board](https://github.com/users/Shaurya-34/projects/2)

## Setup

_Coming soon._

## License

TBD (see #1).
