#!/usr/bin/env python3
"""Claude Console backend - a tiny tmux session manager for the ttyd web terminal.

Binds 127.0.0.1:7682 only. It is never exposed directly; Caddy fronts it on
code.jimmyyu888.com behind the same Tailscale/LAN IP allowlist as the terminal.
The SPA in ./site uses this JSON API to list, create, stop, resume and
remote-control the Claude Code sessions that live in tmux on jimmynas.

Security model:
  * Session names are constrained to ^[A-Za-z0-9_-]{1,24}$.
  * Every tmux call is an argv list (never a shell string) so a name cannot
    inject arguments. The session command string is built from constants and
    shlex-quoted.
  * The only free-form keystrokes ever sent to a pane are the fixed literal
    "/remote-control" slash command.
All sessions are named "claude" (the default/main one) or "claude-<label>".
"""
import json
import os
import posixpath
import re
import shlex
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Best-effort note of which sessions we've started /remote-control in this run.
# Claude owns the real toggle inside the pane; this only drives the UI badge.
REMOTE_ON = set()

SITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site")
MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".map": "application/json",
}

CLAUDE = "/home/jimmy/.local/bin/claude"
# Alias, not a pinned version: "opus" always resolves to the newest Opus-tier
# model, so a future release is picked up without editing this file.
MODEL = "opus"
EFFORT = "max"
WORKDIR = "/srv/appdata"
PREFIX = "claude"  # main session is exactly "claude"; extras are "claude-<label>"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
US = "|"  # field delimiter for tmux -F (safe: names are [A-Za-z0-9_-], rest digits)

# tmux must talk to jimmy's own server (same socket the ttyd service uses) so
# both see the same sessions. Running as jimmy with HOME set is enough.
ENV = dict(os.environ)
ENV.setdefault("HOME", "/home/jimmy")
ENV["PATH"] = "/home/jimmy/.local/bin:" + ENV.get("PATH", "/usr/bin:/bin")


def tmux(*args, check=True):
    """Run a tmux command as an argv list and return (rc, stdout)."""
    p = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, env=ENV, timeout=15
    )
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"tmux {args[0]} failed")
    return p.returncode, p.stdout


def full_name(label):
    """Map a UI label to a real tmux session name. '' / 'main' -> the main one."""
    if label in ("", "main", PREFIX):
        return PREFIX
    if not NAME_RE.match(label):
        raise ValueError("bad session name")
    return f"{PREFIX}-{label}"


def label_of(name):
    if name == PREFIX:
        return ""
    return name[len(PREFIX) + 1:] if name.startswith(PREFIX + "-") else name


def display_name(name):
    """Friendly name shown on the Console card. Used as the session's -n display
    name AND the Remote Control title, so all three stay in sync ('title统一')."""
    return "main" if name == PREFIX else label_of(name)


def session_cmd(name, resume=False):
    """The command for a new session: -n <display> keeps the terminal/title in
    sync with the Console card, and --remote-control rc-<display> turns Remote
    Control ON by default (named to match) so the session is reachable from the
    claude.ai app the moment it starts. Optionally --continue. Quoted for tmux."""
    disp = display_name(name)
    argv = [CLAUDE, "--model", MODEL, "--effort", EFFORT,
            "--dangerously-skip-permissions",
            "-n", disp, "--remote-control", "rc-" + disp]
    if resume:
        argv.insert(1, "--continue")
    return " ".join(shlex.quote(a) for a in argv)


def session_index():
    """Map of session_name -> {id, pids} built from SERVER-WIDE listings.

    Per-session '-t <name>' targeting is unreliable from this daemon: with no
    attached client, tmux resolves a bare *or* '='-prefixed target to the most
    recently active session (so '-t claude' can hit 'claude-z5'). Listing every
    session/pane once and matching names exactly in Python avoids that entirely.
    Actions then target the unique '#{session_id}' ($N), which never collides."""
    idx = {}
    _, out = tmux("list-sessions", "-F", US.join(["#{session_name}", "#{session_id}"]), check=False)
    for line in out.splitlines():
        parts = line.split(US)
        if len(parts) == 2:
            idx[parts[0]] = {"id": parts[1], "pids": []}
    _, out = tmux("list-panes", "-a", "-F", US.join(["#{session_name}", "#{pane_pid}"]), check=False)
    for line in out.splitlines():
        parts = line.split(US)
        if len(parts) == 2 and parts[1].isdigit() and parts[0] in idx:
            idx[parts[0]]["pids"].append(int(parts[1]))
    return idx


def pane_pids(name):
    """PIDs of the foreground process in every pane of the session."""
    return session_index().get(name, {}).get("pids", [])


def cgroup_path(pid):
    """The cgroup v2 path a pid lives in (the '0::' line of /proc/pid/cgroup)."""
    try:
        for line in open(f"/proc/{pid}/cgroup"):
            line = line.strip()
            if line.startswith("0::"):
                return line[3:]
    except OSError:
        pass
    return None


def session_status(name):
    """'paused' if the session's cgroup is frozen by the v2 freezer, else 'running'.
    tmux revives SIGSTOP'd panes, so pause is done via the cgroup freezer (see
    /usr/local/bin/claude-session-freeze); the frozen flag is the source of truth."""
    pids = pane_pids(name)
    if not pids:
        return "running"
    p = cgroup_path(pids[0])
    if not p:
        return "running"
    try:
        for line in open(f"/sys/fs/cgroup{p}/cgroup.events"):
            if line.startswith("frozen"):
                return "paused" if line.split()[1] == "1" else "running"
    except OSError:
        pass
    return "running"


def freeze_session(name, act):
    """Freeze ('freeze') or unfreeze ('thaw') a session via the root helper.
    The helper is the only thing allowed to touch cgroups; we just shell to it."""
    p = subprocess.run(
        ["sudo", "-n", "/usr/local/bin/claude-session-freeze", act, name],
        capture_output=True, text=True, env=ENV, timeout=15,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or f"{act} failed")


def forget_session(name):
    """Drop a session from the boot-time restore roster.

    Killing a session only removes it from tmux; claude-sessions would still
    have it on file and bring it back at the next boot, so a delete has to say
    so explicitly. Absence from tmux deliberately does NOT mean "deleted" there
    — a session that merely crashed must still be restored. Best effort: the
    Console is useful even if the restore tooling is not installed.
    """
    try:
        subprocess.run(
            ["/usr/local/sbin/claude-sessions", "forget", display_name(name)],
            capture_output=True, text=True, env=ENV, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def remote_sessions():
    """Session names whose claude process was launched with --remote-control.

    Read from the process's own argv, not just the REMOTE_ON set: sessions also
    get started outside this daemon — by hand, or by claude-sessions-restore at
    boot — and those launch with --remote-control already on, yet an in-memory
    set would report every one of them as OFF. REMOTE_ON still covers the other
    direction, a session switched on at runtime via the /remote-control command,
    which leaves no trace in argv.
    """
    ppids, argvs = {}, {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argvs[pid] = fh.read().split(b"\0")
            with open(f"/proc/{pid}/stat", "rb") as fh:
                stat = fh.read().decode("utf-8", "replace")
            # comm may contain spaces and ")", so parse after the LAST ")".
            ppids[pid] = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            argvs.pop(pid, None)
    children = {}
    for pid, ppid in ppids.items():
        children.setdefault(ppid, []).append(pid)

    def has_flag(root):
        """The pane's process is a wrapper shell, so scan its descendants too."""
        queue, seen = [root], set()
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            if b"--remote-control" in argvs.get(pid, []):
                return True
            queue.extend(children.get(pid, []))
        return False

    on = set()
    for name, info in session_index().items():
        if any(has_flag(pid) for pid in info["pids"]):
            on.add(name)
    return on


def session_fast(name):
    """True if Fast mode is ON. Detected from the '↯' glyph in the bottom status
    separator — the '──── [↯ ] <name> ──' rule. That line carries ↯ iff fast is
    on (verified: it flips reliably with the toggle), unlike the scrollback."""
    info = session_index().get(name)
    if not info:
        return False
    _, out = tmux("capture-pane", "-p", "-t", info["id"], check=False)
    needle = display_name(name) + " ─"  # "<name> ─" only occurs in that rule
    for line in reversed(out.splitlines()):
        if needle in line:
            return "↯" in line  # ↯
    return False


def list_sessions():
    rc, out = tmux(
        "list-sessions", "-F",
        US.join(["#{session_name}", "#{session_created}",
                 "#{session_attached}", "#{session_activity}"]),
        check=False,
    )
    sessions = []
    remote_on = remote_sessions()  # one /proc walk for the whole listing
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(US)
            if len(parts) != 4:
                continue
            name, created, attached, activity = parts
            if name != PREFIX and not name.startswith(PREFIX + "-"):
                continue
            lbl = label_of(name)
            sessions.append({
                "name": name,
                "label": lbl,
                "display": "main" if name == PREFIX else lbl,
                "created": int(created or 0),
                "activity": int(activity or 0),
                "attached": (attached or "0") != "0",
                "isMain": name == PREFIX,
                "status": session_status(name),
                "remote": name in remote_on or name in REMOTE_ON,
                "fast": session_fast(name),
            })
    sessions.sort(key=lambda s: (not s["isMain"], -s["activity"]))
    return sessions


def next_free_label(existing):
    taken = {s["label"] for s in existing}
    n = 2
    while str(n) in taken:
        n += 1
    return str(n)


def create_session(label, resume):
    """Pre-create a detached session so ttyd's `new-session -A` attaches to it.
    Returns the label the SPA should open in the terminal."""
    name = full_name(label)
    if name in session_index():
        return label_of(name)  # already exists; just attach
    tmux("new-session", "-d", "-s", name, "-c", WORKDIR, session_cmd(name, resume))
    REMOTE_ON.add(name)  # new sessions launch with --remote-control on by default
    return label_of(name)


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeConsole/1.0"

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def log_message(self, *a):  # quiet
        pass

    def _send_file(self, fpath):
        try:
            with open(fpath, "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, {"error": "not found"})
        ext = os.path.splitext(fpath)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path):
        # Strip query, normalise, and confine strictly inside SITE.
        rel = posixpath.normpath(path.split("?", 1)[0].lstrip("/"))
        if rel in ("", "."):
            rel = "index.html"
        fpath = os.path.realpath(os.path.join(SITE, rel))
        if not (fpath == SITE or fpath.startswith(SITE + os.sep)):
            return self._send(403, {"error": "forbidden"})
        if not os.path.isfile(fpath):
            # SPA fallback so deep links still load index.html.
            fpath = os.path.join(SITE, "index.html")
        return self._send_file(fpath)

    def do_GET(self):
        if self.path.rstrip("/") == "/api/sessions":
            try:
                return self._send(200, {"sessions": list_sessions()})
            except Exception as e:  # pragma: no cover
                return self._send(500, {"error": str(e)})
        if self.path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        return self._serve_static(self.path)

    def do_POST(self):
        m = re.match(r"^/api/sessions/([^/]+)/(remote|pause|resume|fast)/?$", self.path)
        if m:
            try:
                name = full_name(m.group(1))
                action = m.group(2)
                info = session_index().get(name)
                if not info:
                    raise RuntimeError("no such session")
                if action == "remote":
                    # Type the slash command into the pane, naming the Remote
                    # Control session "rc-<card>" so every Console-started remote
                    # session is consistently prefixed/grouped in the claude.ai app.
                    tmux("send-keys", "-t", info["id"],
                         "/remote-control rc-" + display_name(name), "Enter")
                    REMOTE_ON.add(name)
                elif action == "fast":
                    # One-click flip: /fast opens the toggle dialog with the CURRENT
                    # state highlighted, so Tab moves to the opposite and Enter
                    # confirms it — reliably flipping regardless of current state.
                    tmux("send-keys", "-t", info["id"], "/fast", "Enter")
                    time.sleep(0.8)
                    tmux("send-keys", "-t", info["id"], "Tab")
                    time.sleep(0.3)
                    tmux("send-keys", "-t", info["id"], "Enter")
                    time.sleep(0.9)
                elif action == "pause":
                    freeze_session(name, "freeze")
                elif action == "resume":
                    freeze_session(name, "thaw")
                return self._send(200, {
                    "ok": True, "label": label_of(name),
                    "status": session_status(name), "fast": session_fast(name),
                })
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if self.path.rstrip("/") == "/api/sessions":
            data = self._body()
            mode = data.get("mode", "new")
            label = (data.get("name") or "").strip()
            try:
                if not label:
                    label = next_free_label(list_sessions())
                if not NAME_RE.match(label):
                    return self._send(400, {"error": "name must be letters, digits, - or _"})
                opened = create_session(label, resume=(mode == "continue"))
                return self._send(200, {"ok": True, "label": opened})
            except Exception as e:
                return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        m = re.match(r"^/api/sessions/([^/]+)/?$", self.path)
        if m:
            try:
                name = full_name(m.group(1))
                info = session_index().get(name)
                if info:
                    # Thaw first so a frozen session's sess-<name> cgroup is torn
                    # down instead of being orphaned when the process dies.
                    try:
                        freeze_session(name, "thaw")
                    except Exception:
                        pass
                    tmux("kill-session", "-t", info["id"])
                REMOTE_ON.discard(name)
                forget_session(name)
                return self._send(200, {"ok": True})
            except Exception as e:
                return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "not found"})


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 7682), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
