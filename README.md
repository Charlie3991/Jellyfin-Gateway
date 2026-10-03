# Jellyfin Gateway

This repository contains published snapshots of Jellyfin Gateway. Development takes place separately, and this copy is updated manually when a new version is ready. The development test suite is not included.

A local web dashboard that ties together **Jellyfin**, **qBittorrent** and
**NordVPN**:

```
┌────────────────────────────────────────────────────────────┐
│  Dashboard  (http://<this-host>:8390)                      │
│                                                            │
│  ● Jellyfin      online v10.x · 214 items                  │
│  ● qBittorrent   online v5.2.3 · ↓ 3.1 MB/s · 4 torrents  │
│  ● NordVPN       connected · Sweden (Solna)                │
│                                                            │
│  + paste magnet / upload .torrent, tag Movie or TV         │
│  + torrent list with live progress & sort status           │
└──────────────────────────────┬─────────────────────────────┘
                               │
        completed torrent ────► automatic sort
                               │
      media/Movies/<Title (Year)>/file.mkv
      media/TV/<Show>/Season NN/file.mkv
```

* **Status cards** — Jellyfin (any reachable server), qBittorrent WebUI
  (version, transfer speeds, DHT, torrent counts) and NordVPN
  (connection, server, subscription expiry, tunnel interface) via the
  `nordvpn` CLI. Jellyfin administrators also get Connect/Disconnect buttons.
* **Jellyfin login** — sign in with an existing Jellyfin username and password.
  The password is sent to Jellyfin once and never stored. The browser receives
  only an opaque, `HttpOnly`, `SameSite=Strict` Gateway session cookie; sessions
  and Jellyfin user tokens remain in memory and disappear on restart.
* **Add torrents** — paste a magnet link (or upload a `.torrent` file) and
  tag it **Movie**, **TV Show**, or **Auto-detect** (any `SxxEyy` marker →
  TV, otherwise Movie). It lands in qBittorrent under the `movies`/`tv`
  category into `media/downloads/`. Uploads are size-capped at the edge:
  a `.torrent` file is limited to 4 MiB (the whole `POST /api/add` body to
  4 MiB + 64 KiB, enforced before the body is buffered) and a magnet link
  to 16 KiB with at most 128 query fields.
* **Automatic sorting** — a background watcher notices completed torrents
  and moves them:
  * movie → `media/Movies/<Title (Year)>/` (main video + matching `.srt`
    sidecars; small samples are left behind)
  * TV, whole season folder → `media/TV/<Show>/Season NN/` (if that
    season folder already exists, the new pack is merged into it
    file-by-file; same-name files get the usual ` (n)` dedupe suffix)
  * TV, loose episodes → grouped into `media/TV/<Show>/Season NN/`
  * unparseable content is stashed under the show/movies folder as-is and
    flagged in the activity feed
  * the real content location is resolved from qBittorrent's per-torrent
    file list (the folder on disk is named after the torrent's content
    name, which for magnets can differ from the displayed name)
  * after a successful sort the torrent is removed from qBittorrent
    (`SORT_DELETE_TORRENT=1`) and Jellyfin is asked to refresh its library
    (needs `JELLYFIN_API_KEY`). Removing the torrent is a separate cleanup
    step from the file move: if qBittorrent refuses the delete, the gateway
    keeps `sort_status=sorted`, records `cleanup_status=pending`, logs a
    warning (never a false "removed"), and the watcher retries *only* the
    delete until it succeeds — the media files are never moved again.
  * sorting is **transactional** — before any file moves, the gateway
    builds a complete sort plan (every file → its final destination,
    dedupe names included) and persists it together with the record. A
    failure mid-sort (disk full, permission error, FS disconnect,
    restart) leaves `sort_status=incomplete` with the plan and the list
    of already-placed files; the watcher (or *Sort now*) **resumes** it:
    placed files are verified and skipped — never re-moved, never
    dedupe-renamed to `(1)`, no duplicates. The source folder is swept
    only after *every* planned file is verified in the library, and the
    torrent is only removed / Jellyfin only refreshed once the whole
    plan has finished. A genuine failure (unparseable content, missing
    files) still stays `failed` and is not auto-retried.
* **VPN guard (kill switch)** — if the tunnel interface (`tun*`/`tap*`/`nordlynx*`)
  disappears, *all* torrents are paused immediately; they resume when the
  tunnel returns. Fail-closed: at startup with no tunnel the torrents are
  actually paused (not just flagged in the UI), and if anything becomes
  active again while the tunnel is still down it is re-paused. The gateway
  and the standalone `vpn_guard.py` service run the *same* guard code.
* **Administrator settings panel** — every `.env` option (qBittorrent / Jellyfin endpoints
  and credentials, media folders, sort behaviour, VPN guard, gateway bind) can
  be edited from the dashboard. Movies, TV and downloads each have their own
  folder field with a **Browse…** button (a folder picker that can reach any
  drive mounted on the box, e.g. `/media/…` or `/mnt/…`). Changes apply to the
  running process immediately (host/port need a restart) and are written back
  to `.env`, so they survive restarts. Passwords and the API key are never
  sent back to the browser — leave a field empty to keep its current value.

## Layout

```
Jellyfin-Gateway/
├── auth.py           # Jellyfin login, session validation, user/admin dependencies
├── gateway.py        # composition root: FastAPI app, routes, UI, re-exports
├── qb.py             # qBittorrent WebUI protocol client
├── state_store.py    # persistent state (records + event log) + state semantics
├── sort_orchestrator.py  # sort pipeline: plan -> execute -> finalize
├── add_verify.py     # add transaction: post-add verification + reconciliation
├── watch.py          # background watcher + VPN-guard integration
├── jellyfin_client.py    # Jellyfin status probe + media refresh
├── service_utils.py  # shared helpers for external-service calls
├── config.py         # env-driven settings
├── sorter.py         # release-name parser + file moving (+ .torrent bdecode)
├── nordvpn.py        # NordVPN CLI wrapper (status/account/connect/disconnect)
├── vpn_guard.py      # VPN guard kill switch (stdlib only); its Guard class
│                     # is shared with gateway.py
├── web/              # dashboard HTML + external JavaScript
├── systemd/          # systemd user units (gateway + standalone VPN guard)
├── media/
│   ├── downloads/    # where qBittorrent puts tagged torrents
│   ├── Movies/       # sorted movies   → point Jellyfin's movie lib here
│   └── TV/           # sorted TV       → point Jellyfin's TV lib here
│                     # (each of the three can live elsewhere, even on
│                      another drive: MOVIES_DIR / TV_DIR / DOWNLOAD_DIR,
│                      or set them in the dashboard's Settings panel)
├── data/             # sort state + activity log (auto-created, gitignored)
└── .env.example      # copy to .env and fill in your credentials
```

## Install

```bash
git clone https://github.com/Charlie3991/Jellyfin-Gateway.git Jellyfin-Gateway && cd Jellyfin-Gateway
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -c requirements.lock
install -m 0600 .env.example .env  # then edit qBittorrent/Jellyfin credentials
```

Requirements: Python 3.11+, a qBittorrent WebUI on the box (desktop GUI or
`qbittorrent-nox`), the `nordvpn` CLI (optional, for the VPN card), and the
`ip` command (standard on Linux).

## Running

The gateway needs the qBittorrent WebUI (port 8090) to be running — the
**desktop qBittorrent GUI** serves it (enable with `WebUI\Enabled=true` in
`~/.config/qBittorrent/qBittorrent.conf`, or let `run.sh` start it), or run
`qbittorrent-nox` headless. The gateway reconnects automatically once the
WebUI is up, so start order does not matter.

**Option A — systemd user units** (recommended, survives logout):

```bash
# edit the gateway unit first (project path, venv python path)
install -m 0644 systemd/jellyfin-gateway.service   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now jellyfin-gateway

systemctl --user status jellyfin-gateway
journalctl --user -u jellyfin-gateway -f                  # logs
loginctl enable-linger                                    # keep running after logout
```

**Option B — manual runner** (no systemd):

```bash
./run.sh                      # starts qBittorrent if needed (GUI if a
                              # display is available, else qbittorrent-nox) + gateway
```

Then open **http://<host-ip>:8390** — on the LAN, or via Tailscale at the
node's tailnet IP (see `TAILSCALE-SETUP.txt`) — and sign in with Jellyfin.

> Note: `qbittorrent` (GUI) and `qbittorrent-nox` share the same config
> directory (`~/.config/qBittorrent/`), so either one works with the same
> port, password and categories. Only run one at a time (single-instance
> lock).

### systemd hardening

Both shipped units carry a conservative defense-in-depth set:
`NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`,
`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK` (AF_NETLINK
is required by the `ip` child; AF_UNIX by the D-Bus-using tailscale/nordvpn
CLIs), `RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`,
`ProtectClock`, `ProtectHostname`, `SystemCallArchitectures=native`, and a
tight `UMask` (0027 gateway / 0077 guard).

Deliberately NOT set: `ProtectSystem`/`ProtectHome`, `SystemCallFilter`,
`MemoryDenyWriteExecute`, `RestrictNamespaces`, `ProtectProc` — the
sorter's media/download dirs are user-configurable and can live anywhere,
and an aggressive seccomp allowlist is inherited by the child CLIs, making
it fragile across upgrades.

Optional (advanced): filesystem confinement via a drop-in.

```bash
systemctl --user edit jellyfin-gateway.service
```

```ini
[Service]
ProtectSystem=strict
# read-only, not `true`: the project and venv live under $HOME and must
# stay readable.
ProtectHome=read-only
# the data dir + every configured media/download location (repeat the
# directive for each; network mounts must be up before the service uses
# them):
ReadWritePaths=%h/Documents/Jellyfin-Gateway/data
ReadWritePaths=/mnt/media
ReadWritePaths=/mnt/downloads
```

Then `systemctl --user daemon-reload && systemctl --user restart
jellyfin-gateway` and check the sorter still works. Keep the secrets tight:
`chmod 600 .env`, `chmod 600 ~/.config/qb-vpn-guard.env`,
`chmod 700 <GATEWAY_DATA_DIR>`. A quick health check on any change:
`systemd-analyze security --user jellyfin-gateway.service`.

## Pointing Jellyfin at the media

Jellyfin can run on this machine or anywhere on the LAN / Tailscale:

1. In Jellyfin: *Dashboard → Libraries* — set the movie library root to
   `…/Jellyfin-Gateway/media/Movies` and the TV library to
   `…/Jellyfin-Gateway/media/TV` (or wherever you pointed `MOVIES_DIR` /
   `TV_DIR`).
2. Set `JELLYFIN_URL` (and optionally `JELLYFIN_API_KEY`) in `.env`, then
   `systemctl --user restart jellyfin-gateway` (or restart `run.sh`).
   The dashboard then shows the Jellyfin version / item count, and every
   successful sort triggers a library refresh.

Without an API key the card still shows online/offline (the status probe
needs no key); the item count and refresh-on-sort just stay off.

## qBittorrent notes

* The gateway creates the `movies` and `tv` categories automatically, both
  saving into `media/downloads/`.
* In **manual** transfer-mode setups the gateway enables the qBittorrent
  preference `use_category_paths_in_manual_mode`, otherwise category save
  paths are ignored and torrents land in the global default download dir.
* Torrents added outside the gateway (e.g. manually in the qBittorrent
  WebUI) are **adopted**: put them in the `movies` or `tv` category and the
  watcher will sort them on completion too.

### qBittorrent WebUI login lockout

qBittorrent may temporarily ban a client IP after repeated failed WebUI
authentication attempts. The common default is 5 failures with a
3600-second ban.

Because the gateway and VPN guard may both authenticate from `127.0.0.1`, a
manual browser login with the wrong password can also temporarily block
those automated clients from establishing a *new* session after a
qBittorrent restart (an already-established session is unaffected; the ban
only hits the login endpoint).

If this happens:

* correct the configured credentials first;
* wait for the ban to expire, or clear it from the qBittorrent WebUI
  settings;
* on machines where several trusted local automated clients share the
  loopback address, consider raising "Ban client after consecutive
  failures" (WebUI settings) to a larger value so one mistype does not
  lock everyone out for an hour.

Do not disable authentication or the failed-login protection simply to
avoid the lockout.

## VPN / privacy

* The VPN guard is defence-in-depth on top of binding qBittorrent to the
  tunnel. For the strongest setup, also set in the qBittorrent WebUI:
  *Settings → Connection → Network Interface → tun0* (the NordVPN
  interface). Then P2P traffic physically cannot leave the real IP.
* While the guard is enabled, Gateway additions are rejected if the tunnel is
  down. New torrents are added stopped and started only after a second tunnel
  check, closing the gap between an add and the next background guard poll.
* Administrators see a qBittorrent warning when it is not bound to the VPN
  interface. The warning's action applies and verifies the interface binding.
* `VPN_GUARD_IFACES` (default `tun*,tap*,nordlynx*`) decides which
  interfaces count as the tunnel. Tailscale is deliberately *not* in the
  default list: it is an always-on overlay, not a P2P VPN. A too-broad glob
  (e.g. `*`) that matches a non-VPN interface such as `lo` or `eth0` is
  rejected at startup and by the Settings API, because it would let the
  guard treat a regular always-up interface as the VPN and silently defeat
  the kill switch. Administrators see the exact matched interfaces in the
  status page.
* The settings panel toggle is **live**: enabling the guard performs the
  fail-closed check immediately (not on the next poll), disabling it resumes
  only the torrents the guard itself paused (manual pauses are untouched) and
  clears that state, and changing `VPN_GUARD_IFACES` re-evaluates the tunnel
  immediately. All of this runs serialized with the background guard thread,
  so a settings change can never race a regular tick.
* The kill switch has **one** implementation (`vpn_guard.Guard`), shared by
  the gateway's background loop and the standalone `vpn_guard.py` service
  (`systemd/qbittorrent-vpn-guard.service`), so both behave identically.
  Run the standalone service only when you are **not** running the gateway —
  never enable both guards at once.
* The dashboard is Jellyfin-authenticated, but it is still designed for a
  private LAN/Tailscale and defaults to `0.0.0.0`. Do not port-forward it
  directly to the internet; use HTTPS through a reverse proxy if exposing it
  beyond a trusted network. The session cookie automatically gains `Secure`
  when the Gateway request is HTTPS.

## Access control

Ordinary Jellyfin users can view service/VPN/torrent/activity status and add
magnets or `.torrent` files. Jellyfin administrators additionally control VPN
connect/disconnect, force sorting, refresh Jellyfin, edit settings, and browse
server folders. Filesystem paths and post-sort destinations are omitted from
ordinary-user API responses.

The Gateway revalidates ordinary sessions with Jellyfin at most every 60
seconds and revalidates before every administrator operation. Disabling a user,
revoking their token, or removing their Jellyfin administrator permission is
therefore enforced by the Gateway. State-changing browser requests also reject
cross-origin submissions.

Treat all torrent metadata and all values returned by qBittorrent, Jellyfin,
the activity log, magnet links, `.torrent` files, and Gateway APIs as hostile.
The dashboard must insert those values with DOM text/property APIs such as
`textContent`, never by interpolating them into HTML strings or inline event
handlers. Dynamic links must keep the `http:`/`https:` protocol allowlist.
The Gateway also sends a Content Security Policy that disallows inline scripts
as a second layer of protection; backend authorization remains mandatory.

`JELLYFIN_API_KEY` remains separate from user login tokens: it is used only for
machine-to-machine library status/refresh work and is never inherited by a
logged-in user.

## Configuration reference (`.env`)

Most of these can also be edited live from the dashboard's **Settings** panel
(written back to `.env` automatically; `GATEWAY_HOST`/`GATEWAY_PORT` take
effect after `systemctl --user restart jellyfin-gateway`). `MEDIA_ROOT`, `NORDVPN_BIN` and
`GATEWAY_TRUSTED_PROXIES` are startup-only: edit `.env` and restart —
`NORDVPN_BIN` is deliberately not dashboard-editable because it selects an
executable, and `GATEWAY_TRUSTED_PROXIES` is not dashboard-editable because it
controls which peers the gateway trusts to forward the real client address.

| Var | Default | Meaning |
|---|---|---|
| `GATEWAY_HOST` / `GATEWAY_PORT` | `0.0.0.0` / `8390` | dashboard bind |
| `HIDE_SETTINGS` | `0` | hide the Settings panel from the dashboard (setup done) |
| `GATEWAY_TRUSTED_PROXIES` | – | trusted reverse-proxy IPs/CIDRs for the login rate limiter (startup-only, not dashboard-editable) |
| `QB_URL` / `QB_USER` / `QB_PASS` | `http://127.0.0.1:8090` / `admin` / – | qBittorrent WebUI |
| `JELLYFIN_URL` | `http://localhost:8096` | Jellyfin base URL |
| `JELLYFIN_API_KEY` | – | enables item count + refresh-on-sort |
| `MEDIA_ROOT` | `<project>/media` | fallback root when no explicit dir is set |
| `MOVIES_DIR` | `$MEDIA_ROOT/Movies` | sorted movies (can be on another drive) |
| `TV_DIR` | `$MEDIA_ROOT/TV` | sorted TV (can be on another drive) |
| `DOWNLOAD_DIR` | `$MEDIA_ROOT/downloads` | where new torrents land before sorting |
| `SORT_DELETE_TORRENT` | `1` | delete torrent after moving; `0` keeps seeding via hardlinks/copies |
| `NOTIFY_JELLYFIN` | `1` | refresh Jellyfin after sort (needs key) |
| `WATCH_POLL` | `3` (s) | completion-watcher interval |
| `VPN_GUARD` | `1` | kill-switch on/off |
| `VPN_GUARD_POLL` | `5` (s) | tunnel check interval |
| `VPN_GUARD_IFACES` | `tun*,tap*,nordlynx*` | interface globs = the tunnel |
| `NORDVPN_BIN` | `nordvpn` | CLI to use for VPN status/actions (startup-only; resolved at boot, not dashboard-editable) |

## Tailscale + NordVPN together

`TAILSCALE-SETUP.txt` documents the full recipe for reaching the dashboard
over Tailscale **while** NordVPN is connected (allowlist the tailnet range,
bypass fwmark so tailscaled's own traffic stays direct), plus a
troubleshooting guide.

## License

MIT — see [LICENSE](LICENSE).
