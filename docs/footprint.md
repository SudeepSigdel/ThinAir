# Footprint

Measured on 2026-11-05 with Docker Compose (Docker 29.8, x86-64, cgroup v2), Thinair 0.1.0.

## Idle (no job running)

| Service | Memory | Limit | CPU |
|---|---|---|---|
| `thinair` (one Python process) | ~77 MiB | 256 MiB | ~0.2 % (a brief spike once a minute from the health check) |
| `docker-proxy` | ~5 MiB | 32 MiB | ~0.1 % |
| **Total** | **~82 MiB** | | |

Nothing polls while idle. Job workers wait on an in-memory queue, and no sandbox container
exists until a job starts. The web app only refreshes its job list while a job is unfinished and
the page is open.

## While a job runs

Each job step runs in its own short-lived sandbox with hard limits:

| Step | Default limits |
|---|---|
| Clone | 1 CPU, 512 MiB |
| Install dependencies (once per job) | 2 CPU, 2 GiB |
| Each profile test run | the tier being tested (e.g. 0.25 CPU / 256 MiB) |
| Agent run (Claude Code) | 2 CPU, 2 GiB, 30 min |

Keep `THINAIR_MAX_CONCURRENT_JOBS=1` on machines with 4 GB of RAM or less.

## Disk

| Image | Size | Built when |
|---|---|---|
| Thinair server | ~280 MB | `docker compose up --build` |
| docker-socket-proxy | ~31 MB | pulled |
| `thinair-sandbox-python` | ~180 MB | first Python job |
| `thinair-sandbox-node` | ~330 MB | first Node job |
| `thinair-sandbox-git` | ~140 MB | first job |
| `thinair-agent-claude-<stack>` | ~1.0 GB each | first Claude Code task for that project type |

Agent images are big because the agent CLI itself is. On a small disk, remove ones you no longer
need: `docker images 'thinair-agent-*'` then `docker rmi <image>`. Images rebuild automatically.

## Startup

A fresh `docker compose build --no-cache` plus start took about 30 s to a healthy server, with the
Python base image already cached. A first-ever install also downloads `python:3.12-slim` (~45 MB).

## A real profile, for scale

`more-itertools` (a pure-Python library with a large test suite): 10 test runs in about 15 minutes.
Its tests take ~60 s per run at 2 CPUs and ~150 s at 0.25 CPU. Result: 0.25 CPU / 512 MiB
minimum (256 MiB ran out of memory at a 258 MiB peak). Recommended: 0.5 CPU / 768 MiB.
