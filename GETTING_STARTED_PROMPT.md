# Getting started prompt

Paste everything below the line into Claude Code (or another coding agent) with this
project folder as the working directory.

---

I've been given this project, award-flight-researcher, and I want to get it running for my
own trips. I don't know yet what my setup should look like, so please work with me one step
at a time and ask before you decide anything that is mine to decide.

## What the project is

It polls the seats.aero API for award flight availability, joins outbound and return legs
into trip pairs, checks each pair against my points balances and limits, and sends an alert
only when a complete round trip works. Read `README.md` first, then `CLAUDE.md` for the
architecture, the config format and the invariants. Treat both as the source of truth. Do
not guess at behavior they describe.

## Ground rules

- Use `uv` for everything: `uv sync`, `uv run researcher ...`, `uv run pytest`. Do not call
  `pip` or `python` directly. If `uv` is not installed, tell me and help me install it for
  my operating system.
- Never commit, print, or paste secrets. `.env`, `config/search.yaml`,
  `config/balances.yaml`, `data/` and any `*.db` file are gitignored on purpose. Keep it that
  way. Use the `.example` files as templates.
- Do not run a polling command until I have confirmed my API key is in `.env` and I
  understand the quota (below).
- Before you change any code, say what you plan to change and why. Most of this should be
  configuration, not code edits.

## Things that are mine to decide, so please ask me

1. **My operating system and what I have installed.** Check for `uv` and a Python version that satisfies `requires-python` in
   `pyproject.toml` (currently 3.10 or newer), and for `git`.
2. **Where it should run.** I may not have a server or homelab. Offer me realistic options
   for my situation and recommend one:
   - Run `uv run researcher run-once` by hand when I want to check.
   - Run `uv run researcher watch --interval-minutes N` on my own computer, in `tmux` or as
     a background service (launchd on macOS, systemd on Linux, Task Scheduler on Windows).
   - A cron job or scheduler on a small always-on machine or VPS.
   - A container, if I already use Docker or Podman.
   - A scheduled CI job. The included `.gitea/workflows/poll.yml` is written for a
     self-hosted Gitea with a MinIO/S3 bucket. If I use GitHub Actions instead, you would
     need to adapt it, and the sqlite database has to be stored somewhere between runs
     (an S3 bucket, a cache, or a committed artifact). Explain that trade-off before
     suggesting it. Skip all of this if I only want to run it locally.
3. **How I want to be alerted.** ntfy (just a topic name, no account) or Pushover (needs a
   paid app and two keys), or neither for now and only read the output of
   `uv run researcher pairs`.
4. **My trip.** Walk me through `config/search.example.yaml` and ask for: number of
   passengers, origin and destination airports, one or more date windows with trip length
   in nights for each, cabins, which seats.aero sources to poll, and filters such as
   minimum seats and direct flights only. Then write `config/search.yaml`.
5. **My points.** Walk me through `config/balances.example.yaml` and ask for my airline
   program balances, my transferable point pools and the programs each one can transfer to,
   plus any cap on fees per passenger and miles per passenger by cabin. Then write
   `config/balances.yaml`. If I would rather not enter real balances yet, use
   placeholder values and tell me clearly that the results will not mean anything until I
   replace them.

## The seats.aero API key and quota

The project needs a seats.aero Pro API key in `.env` as `SEATSAERO_API_KEY`. I have to get
that myself from my seats.aero account; tell me where to look if I'm unsure. The Pro quota
is 1000 API calls per calendar day (resets at midnight UTC). A cycle costs roughly
(date windows x routes x directions) search calls, plus one call for every newly seen
flight. Before I run anything on a schedule, estimate my calls per cycle from my config,
and use that to suggest a polling interval that stays well under 1000 per day. A 429 from
the API aborts the cycle by design.

## Steps, in order

1. Read `README.md` and `CLAUDE.md`, then summarize in a few sentences how the pipeline
   works so I can check we understand it the same way.
2. Check my prerequisites and run `uv sync`.
3. Run `uv run pytest` and show me the result. All tests should pass before we go further.
4. Ask me the questions above, then create `.env`, `config/search.yaml` and
   `config/balances.yaml`.
5. Run `uv run researcher init-db`.
6. Do a first `uv run researcher run-once`, report what it did and what it spent from the
   quota, and show me `uv run researcher pairs --state viable --limit 20`.
7. Test my alert channel once (send a harmless test message the way `alerts.py` does, or
   help me with a one-off publish to my ntfy topic) so I know alerts will reach me.
8. Set up the schedule or long-running process we agreed on, and tell me how to check that
   it is running, how to stop it, and where the database file lives so I can back it up.

After each step, tell me what happened and wait for me before you continue. If something
fails, show me the actual error and your diagnosis. Do not work around it silently.
