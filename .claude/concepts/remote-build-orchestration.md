# Remote Build Orchestration

## Problem

The data machine (desktop, Nobara/amd64) builds; the Mac develops. Today
that is three manual commands across two machines — `update_map.sh` then
`sync_to_mac.sh` on the desktop, `post_sync.sh` on the Mac — and it only
works from the home LAN. Away from home the desktop is unreachable, so no
data refresh happens at all. Even at home, a dropped SSH session kills a
running build, and a dropped link aborts an in-flight ~12 GB transfer with
no retry.

The goal: one command on the Mac, from anywhere, that leaves the Mac
serving fresh data hours later without further attention.

## Requirements

### Reachability

- Both machines join a Tailscale tailnet. The desktop is addressed by a
  stable tailnet name from anywhere — no DynDNS, no port forwarding, no
  SSH exposed to the public internet.
- The desktop requires power only. No graphical login, no interactive
  session, nothing pinned to a logged-in desktop environment.
- Reverse trust: after this change the desktop needs no credentials for,
  and no knowledge of, the Mac. `MAC_REMOTE` / `MAC_PATH` and the
  desktop's `mac` SSH alias cease to exist.

### Single Mac-side entry point

- One new Mac-side script, **`scripts/remote_build.sh`**, is the only
  command the user runs. It orchestrates four phases:
  **launch → watch → fetch → post-sync**.
- Accepts and forwards the build's own flags (`--osm`, `--skip-deploy`).
- Phase entry flags so a failed or interrupted run resumes rather than
  restarts: `--watch-only`, `--fetch-only`, `--no-post-sync`, plus
  `--dry-run` reporting the decisions without acting.
- The final terminal output is an unambiguous verdict — one success/fail
  line plus per-phase wall times, matching the style `update_map.sh`
  already uses.

### Detached build

- The build must not be parented to the SSH session. It survives Mac
  sleep, a closed lid, a network change, and the orchestrator itself
  being killed.
- It runs in a named tmux session on the desktop. Invoking
  `remote_build.sh` while a build is live attaches to it and watches,
  rather than starting a second one. Two concurrent builds must be
  impossible.
- The desktop-side wrapper writes two artifacts next to the repo:
  - a **full log** of the run,
  - a **status stamp** written only on completion, carrying the build's
    real exit code.
- The stamp is the sole completion and success signal. The log tail is
  never used to infer either.
- The exit code must survive the pipe into the log (the build script has
  no built-in logging and is meant to be run under `tee`; a naive pipe
  reports `tee`'s status instead).
- The launch phase invalidates any previous stamp before starting, so a
  stale stamp can never be read as this run's result.

### Log streaming

- The live log streams to the Mac console and reads like a local run.
- Streaming tolerates interruption: it reconnects and resumes rather than
  failing the orchestrator. The authoritative complete log stays on the
  desktop.
- The stream is also teed to a Mac-side copy, and on failure the
  desktop's log is fetched down whole so it is readable offline.
- Losing the stream is never itself a failure — only the stamp decides.

### Pull-based fetch

- Transfer direction reverses: the Mac fetches from the desktop. The
  push-based `sync_to_mac.sh` is replaced by a Mac-side fetch (either a
  new script or a mode of `remote_build.sh`).
- Carried over **unchanged** from the existing push script, because this
  part encodes past incidents: the five group definitions (`assets`,
  `motis`, `valhalla`, `lookup`, `routed`), their include/exclude filter
  chains, the per-group compression choices, the `--delete` group set,
  and the sentinel filenames.
- Sentinels are checked on the desktop before each group; a missing
  sentinel skips that group with a warning instead of mirroring an
  interrupted build.
- `--delete` now applies locally, on the machine the user is sitting at,
  gated by those same sentinel checks.
- The fetch refuses to run while a build is live on the desktop;
  `--force` overrides.
- Each group retries on failure, resuming mid-file rather than restarting
  a multi-GB index. A stalled connection must fail fast rather than hang
  indefinitely.

### Completion

- On a successful fetch the orchestrator runs `post_sync.sh` locally,
  unchanged, including its sidecar rebuild, import decision, ordered
  service restart, and smoke query.
- Success therefore means "the Mac serves the new data", not "files
  arrived".
- Any phase failing stops the run before the next phase. A partial fetch
  must never reach `post_sync.sh`.

## Constraints

- Unchanged: `update_map.sh`, `post_sync.sh`, `rebuild_transit.sh`,
  `setup_routing.sh`, and all four deploy scripts. Production deploys
  still happen from the desktop at the end of the build; the app deploy
  stays the push-to-`main` GitHub Action.
- GNU rsync must be installed on the Mac before the direction flip. The
  bundled openrsync 2.6.9 works as a passive receiver (the desktop's GNU
  rsync drives the filters today), but as the filter-driving client its
  rule support is unreliable — silent filter misses, not errors.
- Feed directories still travel whole or not at all. The sidecar
  (`data/gtfs_motis/`) is still never synced, only rebuilt.
- Wake-on-LAN is deliberately out of scope. The desktop has sleep
  disabled and availability is assumed. Waking a powered-off desktop is
  separate later work and needs an always-on LAN sentinel, since magic
  packets must originate inside the LAN.
- Push notification on completion is out of scope. The console verdict is
  the notification.
- A GitHub Actions self-hosted runner was considered and rejected: it
  cannot carry ~12 GB of artifacts, so it would add a second control
  plane without removing the SSH/Tailscale data path.
- The Mac may sleep during the build but must stay awake for the fetch
  and post-sync legs.

## Desktop prerequisites (one-time, run by hand)

Each item is a check first; act only if it fails. The acceptance test for
the whole list is the last item.

1. **Tailscale** — install, `sudo tailscale up`, then
   `systemctl is-enabled tailscaled` must say `enabled`. Reboot and
   confirm the machine is on the tailnet *without* logging in. Note its
   tailnet hostname.
2. **SSH** — `systemctl is-enabled sshd` must say `enabled`. Add the
   Mac's public key to `~/.ssh/authorized_keys`. Confirm login works
   while the desktop sits at its login screen with no session open.
3. **Docker must be the system daemon** — `systemctl is-enabled docker`
   must say `enabled`, and `id -nG` must list `docker`. If Docker is
   rootless or a `systemd --user` unit, it stops at logout: run
   `loginctl enable-linger $USER`. Verify by starting a container over
   SSH with nobody logged in locally.
4. **Deploy key usable headless** — from a bare SSH session (no
   graphical login anywhere), `ssh koramaps true` must succeed without
   prompting. If it asks for a passphrase, the key is being unlocked by
   gnome-keyring at graphical login and the deploy phase will fail
   unattended. Fix with an unencrypted key or a lingering agent.
5. **tmux** — `command -v tmux`. Install if missing.
6. **Sleep off** — confirm `systemctl status sleep.target suspend.target`
   shows them masked or otherwise disabled. Optionally set the BIOS to
   restore power state after an AC loss.
7. **Repo state** — the checkout sits at a known path, tracks the branch
   it should build, and `git pull --ff-only` runs clean. Local
   modifications that would block a fast-forward must be resolved or
   committed.
8. **Disk headroom** — enough free space for a full run, including the
   ~12 GB of artifacts the Mac will later fetch.
9. **Acceptance test** — with no local session open, SSH in and run the
   build detached with `--skip-deploy`, disconnect, reconnect later, and
   confirm it ran to completion and wrote a zero exit code.
10. **Cleanup, after the pull flip is in place** — remove the desktop's
    `mac` SSH alias and any key it holds for the Mac.

## Mac prerequisites

- Tailscale installed and joined to the same tailnet.
- GNU rsync installed (`brew install rsync`) and confirmed ahead of the
  filter-driving pull.
- SSH alias for the desktop's tailnet name in `~/.ssh/config`.
- Amphetamine (already in use) to cover the fetch and post-sync legs.
