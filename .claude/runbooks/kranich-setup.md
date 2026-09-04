# Kranich — one-time setup for remote-triggered builds

Paste everything below into a chat running **on Kranich**.

---

## Context

Kranich (Nobara / amd64) is the data machine for the Kora Maps project.
It runs the transit pipeline, the Valhalla tile build, the footpath
matrix, the MOTIS import, and the production deploys. A MacBook develops
the code and will soon trigger builds on Kranich over SSH from anywhere,
walk away, and pull the finished artifacts back.

For that to work Kranich must be usable **while nobody is logged in
graphically** — powered on and at the login screen is the normal state
during a build. Sleep must therefore be impossible in *that* state, which
is not the same as it being off in yours: see item 6, which is the one
that already caused a stalled build. Wake-on-LAN is explicitly out of
scope for now.

Your job is to verify (and where needed establish) that headless
readiness, then report back. **Check first, act only where a check
fails.** Do not change pipeline code or project files — this is host
configuration only.

## Tasks

### 1. Tailscale

- Install Tailscale if absent, then `sudo tailscale up`.
- `systemctl is-enabled tailscaled` must report `enabled`.
- Reboot and confirm Kranich is on the tailnet **without anyone logging
  in**. This is the important part — a Tailscale that only comes up after
  a graphical login is useless here.
- Report the exact tailnet hostname and the `100.x` address.

### 2. SSH

- `systemctl is-enabled sshd` must report `enabled`.
- Key-based login must work. The MacBook's public key will be supplied
  separately; if it is not in `~/.ssh/authorized_keys` yet, say so and
  leave a note where it should go.
- Confirm SSH login succeeds while Kranich sits at the login screen with
  no session open.
- Report whether password authentication is enabled (it should not be).

### 3. Docker must be the system daemon

This is the most likely thing to be wrong.

- `systemctl is-enabled docker` must report `enabled`.
- `id -nG` must include `docker`.
- If Docker is rootless, or runs as a `systemd --user` unit, it stops
  when the user logs out. In that case run `loginctl enable-linger $USER`
  and re-verify.
- Verify properly: with **no local graphical session**, SSH in and run a
  throwaway container (`docker run --rm hello-world`). If that fails, the
  build will fail hours in.
- Report which mode Docker is in.

### 4. Deploy key usable headlessly

The build's last phase deploys to the production VPS over the SSH alias
`koramaps`.

- From a bare SSH session with **no graphical login anywhere**, run
  `ssh -o BatchMode=yes koramaps true`.
- It must succeed silently. If it prompts for a passphrase or fails, the
  key is being unlocked by gnome-keyring at graphical login and every
  unattended deploy will hang or fail.
- If it fails, report how the key is currently protected. Do not
  unilaterally replace it with an unencrypted key — report first.

### 5. tmux

- `command -v tmux`; install if missing. Builds will run inside a named
  tmux session so they survive a dropped SSH connection.

### 6. Sleep must be impossible, not merely unconfigured

The single most dangerous item on this list, and the one that already bit
us. A build that suspends stalls silently and indefinitely: the exit-code
stamp never appears, the Mac's watch loop reconnects forever, and nothing
says why.

**Do not use `systemctl is-enabled` to judge this.** For these units it
returns `static` — meaning "no [Install] section", not "disabled". It says
`static` on a machine that suspends every 15 minutes and on one that
cannot suspend at all. It is not a check.

Two things make the naive check especially misleading:

- **Being logged in proves nothing.** Power settings are per session. A
  desktop that has run scripts for days without sleeping while you were
  logged in can still suspend at the login screen, which is exactly the
  state it sits in during every remote build.
- **On this machine the greeter was set to suspend.** GDM's session
  reported `sleep-inactive-ac-type = 'suspend'` while the logged-in user's
  was `'nothing'`, and `/etc/dconf/db/gdm.d/` was empty, so it inherited
  the stock GNOME default. GNOME's power plugin goes by *session idle*,
  not CPU load, so a running build does not hold it off.

**The fix is masking**, because it is the only layer that does not depend
on which session's settings are in effect — user, greeter, or none:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

Verify with `systemctl is-enabled sleep.target` → must print `masked`.
That is the check; `static` is a failure, not a pass.

Optional supporting settings:

- **Power button.** A press suspends a running machine by default.
  Nobara has no `/etc/systemd/logind.conf`, so use a drop-in:
  ```bash
  sudo mkdir -p /etc/systemd/logind.conf.d
  printf '[Login]\nHandlePowerKey=ignore\n' | sudo tee /etc/systemd/logind.conf.d/10-kora.conf
  sudo systemctl restart systemd-logind
  ```
  This does not affect waking from suspend — that is a firmware/ACPI
  event — only what a press does while the machine is running. You do
  lose press-to-shutdown; use `systemctl poweroff` instead.
- **BIOS/UEFI**: restore power state after an AC loss, so a power cut does
  not leave Kranich off indefinitely.

Diagnostics worth recording if any of this looks wrong:
`cat /sys/power/state` (what the hardware can do), and
`sudo -u gdm dbus-run-session gsettings get org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type`
(read-only — what the greeter will do).

### 7. Repo state

- Report the **absolute path** of the Kora Maps checkout.
- Report which branch it tracks and whether `git pull --ff-only` runs
  clean. Uncommitted local modifications that would block a
  fast-forward need to be reported, not discarded.
- Report the current `git log -1 --oneline`.

### 8. Disk headroom

- Report free space on the filesystem holding the checkout. A full run
  needs room for the OSM extracts, the routed feed, the Valhalla tiles,
  the MOTIS indexes and the footpath matrix — and the MacBook will later
  fetch roughly 12 GB of it.

### 9. Acceptance test

The real proof, and the last step:

- With no local session open, SSH in from elsewhere.
- Start the build detached and deploy-free, logging to a file, e.g.
  `tmux new -d -s build 'cd <repo> && ./scripts/update_map.sh --skip-deploy 2>&1 | tee update.log'`
- Disconnect the SSH session entirely.
- Reconnect later, reattach (`tmux attach -t build`), and confirm the run
  completed and reported success.
- Report the total wall time and the per-stage timing table the script
  prints at the end.

Note: the build script takes branch flags (`--only-pipeline`,
`--only-routing`) and `--skip-gtfs`; its helper scripts live in
`scripts/routing/` and `scripts/deploy/`. For a quicker acceptance test
than a full build, `--only-pipeline --skip-gtfs --skip-deploy` exercises
the same machinery without downloads, matrix or deploy.

## Report back

1. Tailnet hostname and `100.x` address.
2. Docker mode, and whether linger was needed.
3. Whether the headless `koramaps` deploy key test passed.
4. Absolute repo path, tracked branch, `git log -1 --oneline`, whether
   the tree is clean.
5. Free disk on the repo's filesystem.
6. `systemctl is-enabled sleep.target` — must be `masked`, not `static`.
7. Acceptance-test result with the timing table.
8. Anything you had to change, and anything you found but did not change.
