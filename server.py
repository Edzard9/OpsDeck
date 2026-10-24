#!/usr/bin/env python3
"""OpsDeck — server staging: file statis + API data VPS asli (stdlib saja).
Bind hanya ke IP Tailscale mesin ini (dideteksi otomatis saat start), port 8091.
Endpoint: /api/stats, /api/agents, /api/logs
"""
import json, os, re, shutil, socket, subprocess, threading, time
from datetime import datetime, timezone, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

WIB = timezone(timedelta(hours=7))

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = 8091

def get_tailscale_ip():
    """Ambil IP Tailscale mesin ini dari OS — tidak pernah diketik manual."""
    try:
        r = subprocess.run(["ip", "-4", "-j", "addr", "show", "tailscale0"],
                           capture_output=True, text=True, timeout=4)
        for iface in json.loads(r.stdout):
            for a in iface.get("addr_info", []):
                if a.get("family") == "inet" and a.get("local", "").startswith("100."):
                    return a["local"]
    except Exception:
        pass
    try:
        r = subprocess.run(["tailscale", "ip", "-4"],
                           capture_output=True, text=True, timeout=4)
        ip = r.stdout.strip().splitlines()[0].strip()
        if ip.startswith("100."):
            return ip
    except Exception:
        pass
    return None

HOST = get_tailscale_ip() or "127.0.0.1"

# ---------- sampler latar: CPU% + laju jaringan + laju disk ----------
_state = {"cpu_pct": 0.0, "net_in": 0.0, "net_out": 0.0, "disk_io": 0.0}

def _cpu_times():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = list(map(int, parts))
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)

def _net_bytes():
    rx = tx = 0
    with open("/proc/net/dev") as f:
        for line in f:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            if name.strip() == "lo":
                continue
            nums = rest.split()
            rx += int(nums[0]); tx += int(nums[8])
    return rx, tx

def _disk_bytes():
    total = 0
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            if len(p) < 14:
                continue
            name = p[2]
            if re.fullmatch(r"(vd|sd|nvme)[a-z0-9]*[a-z](\d*)?", name) and not re.search(r"p?\d+$", name):
                total += (int(p[5]) + int(p[9])) * 512
    return total

def _sampler():
    pc, pn, pd = _cpu_times(), _net_bytes(), _disk_bytes()
    pt = time.time()
    while True:
        time.sleep(2)
        c, n, d = _cpu_times(), _net_bytes(), _disk_bytes()
        t = time.time(); dt = t - pt
        di, dt_total = c[0] - pc[0], c[1] - pc[1]
        if dt_total > 0:
            _state["cpu_pct"] = round(max(0.0, min(100.0, (1 - di / dt_total) * 100)), 1)
        _state["net_in"] = round((n[0] - pn[0]) / dt / 1048576, 3)
        _state["net_out"] = round((n[1] - pn[1]) / dt / 1048576, 3)
        _state["disk_io"] = round((d - pd) / dt / 1048576, 2)
        pc, pn, pd, pt = c, n, d, t

threading.Thread(target=_sampler, daemon=True).start()

# ---------- endpoint data ----------
def stats():
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    used = total - avail
    disk = shutil.disk_usage("/")
    docker = "absent"
    if shutil.which("docker"):
        try:
            r = subprocess.run(["systemctl", "is-active", "docker"],
                               capture_output=True, text=True, timeout=3)
            docker = "running" if r.stdout.strip() == "active" else "installed"
        except Exception:
            docker = "installed"
    return {
        "hostname": socket.gethostname(),
        "ip": HOST,
        "cpu_cores": os.cpu_count() or 0,
        "cpu_pct": _state["cpu_pct"],
        "mem_total_gb": round(total / 1073741824, 2),
        "mem_used_gb": round(used / 1073741824, 2),
        "mem_pct": round(used / total * 100, 1) if total else 0,
        "disk_total_gb": round(disk.total / 1073741824, 1),
        "disk_used_gb": round(disk.used / 1073741824, 1),
        "disk_pct": round(disk.used / disk.total * 100, 1) if disk.total else 0,
        "disk_io_mbs": _state["disk_io"],
        "net_in_mbs": _state["net_in"],
        "net_out_mbs": _state["net_out"],
        "load1": round(os.getloadavg()[0], 2),
        "uptime_s": int(float(open("/proc/uptime").read().split()[0])),
        "docker": docker,
    }

RULES = [
    ("opencode",            "opencode — Build Agent",      "AI coding agent",            "fa-code"),
    ("code-server",         "code-server",                 "VS Code di browser",         "fa-laptop-code"),
    ("server.py",           "OpsDeck Server",              "Server dashboard ini (:8091)","fa-gauge-high"),
    ("http.server 8090",    "Preview Porto-baru",          "Server preview statis (:8090)","fa-globe"),
    ("cloudflared",         "Cloudflare Tunnel",           "Tunnel layanan",             "fa-cloud"),
    ("caddy",               "Caddy",                       "Reverse proxy",              "fa-server"),
    ("tailscaled",          "Tailscale",                   "Daemon mesh VPN",            "fa-network-wired"),
    ("9router",             "9Router",                     "Gateway model (panel)",      "fa-route"),
    ("next-server",         "Next.js Server",              "Aplikasi web Node",          "fa-layer-group"),
]

def agents():
    try:
        out = subprocess.run(["ps", "-eo", "etime=,%cpu=,%mem=,rss=,args="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    groups = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\S+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(.*)", line)
        if not m:
            continue
        etime, cpu, _mem, rss, args = m.groups()
        for key, name, sub, icon in RULES:
            if key in args:
                g = groups.setdefault(name, {"name": name, "sub": sub, "icon": icon,
                                               "uptime": etime, "cpu": 0.0, "rss": 0})
                g["cpu"] += float(cpu); g["rss"] += int(rss)
                break
    rows = []
    for g in sorted(groups.values(), key=lambda x: -x["rss"]):
        mb = g["rss"] / 1024
        g["mem"] = f"{mb/1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"
        g["cpu"] = round(g["cpu"], 1)
        del g["rss"]
        rows.append(g)
    return rows[:8]

def logs():
    try:
        r = subprocess.run(["journalctl", "-n", "45", "--no-pager", "-o", "short-iso"],
                           capture_output=True, text=True, timeout=5)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if lines:
            return lines
    except Exception:
        pass
    try:
        with open("/var/log/syslog") as f:
            return [l.rstrip() for l in f.readlines()[-45:] if l.strip()]
    except Exception:
        return ["(log tidak tersedia)"]

# ---------- Mode Awam: terjemahan log berbasis aturan (tanpa AI, 0 token) ----------
_LINE = re.compile(r"^(\S+)\s+\S+\s+([^:\[\s]+)(?:\[\d+\])?:\s*(.*)$")

def _feed_event(unit, msg):
    """Kembalikan (kunci_grup, level, teks_awam) untuk satu baris log."""
    m = msg
    if unit.startswith("sshd"):
        if "Failed password" in m or "authentication failure" in m:
            return ("ssh-gagal", "waspada",
                    "Ada percobaan masuk SSH yang gagal dari internet. Ini kebisingan biasa di internet — bukan dari perangkatmu, dan percobaannya gagal.")
        if "Accepted" in m:
            return ("ssh-ok", "baik", "Ada login SSH yang berhasil masuk (kemungkinan kamu atau perangkatmu sendiri).")
        return ("ssh-lain", "info", "Aktivitas SSH biasa (sesi dibuka/ditutup).")
    if unit.startswith("cloudflared"):
        if "ERR" in m or "Failed" in m or "error" in m.lower():
            return ("cf-error", "waspada",
                    "Tunnel Cloudflare sempat gagal menyambung dan sedang mencoba lagi otomatis. Layanan yang lewat tunnel bisa tersendat sebentar.")
        if "precheck complete" in m and "hard_fail=true" in m:
            return ("cf-precheck", "waspada",
                    "Pemeriksaan awal Cloudflare menemukan kendala jaringan, jadi tunnel belum sepenuhnya sehat.")
        if "Registered tunnel connection" in m:
            return ("cf-ok", "baik", "Tunnel Cloudflare tersambung dengan baik.")
        return ("cf-info", "info", "Cloudflare Tunnel: aktivitas normal.")
    if unit.startswith("tailscaled"):
        return ("ts-info", "info", "Jaringan Tailscale aktif — perangkat-perangkatmu saling terhubung seperti biasa.")
    if unit.startswith("systemd-timesyncd"):
        return ("time-info", "info", "Sinkronisasi jam internet sempat tersendat. Biasanya pulih sendiri dan tidak mengganggu.")
    if unit.startswith("code-server"):
        return ("cs-info", "info", "code-server (VS Code di browser): aktivitas biasa.")
    if "oom" in m.lower() or "out of memory" in m.lower():
        return ("oom", "bahaya", "RAM hampir habis — sistem mungkin mematikan proses. Perlu dicek bila sering terjadi.")
    if unit.startswith("systemd"):
        st = re.search(r"Started\s+(\S+)", m)
        if st:
            return ("svc-" + st.group(1), "info", f"Layanan {st.group(1)} mulai berjalan.")
        if "Failed" in m or "failed" in m.lower():
            return ("svc-gagal", "bahaya", "Ada layanan sistem yang gagal. Detailnya ada di mode Teknis.")
        return ("svc-info", "info", "Aktivitas layanan sistem biasa.")
    if re.search(r"\b(error|fail|warn)", m, re.I):
        return ("err-" + unit, "waspada", f"Ada pesan error/peringatan dari {unit}. Detail teknisnya bisa dilihat di mode Teknis.")
    ringkas = m if len(m) <= 140 else m[:137] + "..."
    return ("lain-" + unit, "info", f"{unit}: {ringkas}")

def feed():
    events = {}
    order = []
    for line in reversed(logs()):
        mm = _LINE.match(line.strip())
        if not mm:
            continue
        ts_raw, unit, msg = mm.groups()
        try:
            jam = datetime.fromisoformat(ts_raw).astimezone(WIB).strftime("%H:%M")
        except Exception:
            jam = "--:--"
        key, level, text = _feed_event(unit, msg)
        if key in events:
            events[key]["count"] += 1
        else:
            events[key] = {"time": jam, "level": level, "text": text, "count": 1, "source": unit}
            order.append(key)
    out = []
    for key in order[:25]:
        e = events[key]
        if e["count"] > 1:
            e = dict(e)
            e["text"] += f" (kejadian serupa: {e['count']}x di jendela log ini)"
        out.append(e)
    return out

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=BASE, **kw)
    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path == "/api/stats":
            return self._json(stats())
        if self.path == "/api/agents":
            return self._json(agents())
        if self.path == "/api/logs":
            return self._json({"lines": logs()})
        if self.path == "/api/feed":
            return self._json({"events": feed()})
        return super().do_GET()
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
