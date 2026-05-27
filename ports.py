"""PortWho — ポート使用状況の列挙と kill（標準ライブラリのみ）

GUI から独立した純ロジック。tkinter に依存しないのでテスト・CLI 流用が容易。

Windows: netstat -ano（ポート→PID）+ tasklist（PID→名前）
         + PowerShell Get-CimInstance（PID→コマンドライン。wmic は使わない）
Unix   : ss -tlnp / lsof（fallback）+ ps
"""
from __future__ import annotations

import json
import locale
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

IS_WIN = sys.platform.startswith("win")

# --windowed ビルド時に各サブプロセスのコンソールが一瞬開くのを防ぐ
_NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW


# ── 内部ユーティリティ ────────────────────────────────────────────────────────

def _run(args, timeout=15):
    """コマンドを実行し stdout を文字列で返す。失敗時は空文字。"""
    try:
        cp = subprocess.run(
            args, capture_output=True, timeout=timeout,
            creationflags=_NO_WINDOW if IS_WIN else 0,
        )
    except Exception:
        return ""
    return _decode(cp.stdout or b"")


def _decode(data: bytes) -> str:
    """端末出力を寛容にデコード（utf-8 → ロケール → cp932 → replace）。"""
    encs = ["utf-8", locale.getpreferredencoding(False) or "utf-8", "cp932"]
    for enc in encs:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


# ── Windows ───────────────────────────────────────────────────────────────────

def _pid_info_win() -> dict[int, dict]:
    """PID → {name, cmdline} を PowerShell Get-CimInstance で一括取得。

    日本語パスを含むコマンドラインがコンソール/パイプのコードページで化けるのを
    避けるため、UTF-8 → Base64 にして受け渡し、Python 側で確実に復号する。
    """
    ps = (
        "$j = Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress;"
        "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j))"
    )
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=20)
    try:
        import base64
        data = json.loads(base64.b64decode(out.strip()).decode("utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict):
        data = [data]
    res: dict[int, dict] = {}
    for o in data:
        pid = o.get("ProcessId")
        if pid is None:
            continue
        res[int(pid)] = {"name": o.get("Name") or "", "cmdline": o.get("CommandLine") or ""}
    return res


def _pid_name_tasklist() -> dict[int, str]:
    """tasklist による PID → 名前（CIM が使えない場合の保険）。"""
    out = _run(["tasklist", "/FO", "CSV", "/NH"])
    res: dict[int, str] = {}
    for line in out.splitlines():
        m = re.match(r'^"([^"]+)","(\d+)"', line)
        if m:
            res[int(m.group(2))] = m.group(1)
    return res


def _listening_win() -> list[dict]:
    net = _run(["netstat", "-ano"])
    cim = _pid_info_win()
    names = _pid_name_tasklist()

    rows: list[dict] = []
    seen: set[tuple] = set()
    for line in net.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] not in ("TCP", "UDP"):
            continue
        proto = parts[0]
        local = parts[1]
        if proto == "TCP":
            if len(parts) < 5 or parts[3] != "LISTENING":
                continue
            state, pid_s = parts[3], parts[4]
        else:  # UDP は State 列が無く foreign が *:*
            state, pid_s = "", parts[-1]
        if ":" not in local or not pid_s.isdigit():
            continue
        port_s = local.rsplit(":", 1)[1]
        if not port_s.isdigit():
            continue
        pid = int(pid_s)
        key = (proto, int(port_s), pid)
        if key in seen:  # IPv4/IPv6 で同一ポートが二重に出るのを集約
            continue
        seen.add(key)
        info = cim.get(pid, {})
        rows.append({
            "proto": proto,
            "port": int(port_s),
            "pid": pid,
            "state": state or "BOUND",
            "name": info.get("name") or names.get(pid, ""),
            "cmdline": info.get("cmdline", ""),
            "local": local,
        })
    return rows


# ── Unix（fallback） ────────────────────────────────────────────────────────────

def _ps_cmdlines() -> dict[int, str]:
    out = _run(["ps", "-eo", "pid=,args="])
    res: dict[int, str] = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(.*)", line)
        if m:
            res[int(m.group(1))] = m.group(2).strip()
    return res


def _listening_unix() -> list[dict]:
    cmds = _ps_cmdlines()
    rows: list[dict] = []
    seen: set[tuple] = set()

    ss = _run(["ss", "-tlnp"])
    if ss.strip():
        for line in ss.splitlines():
            m = re.search(
                r"LISTEN\s+\d+\s+\d+\s+\S+:(\d+)\s+\S+\s+users:\(\(\"([^\"]+)\",pid=(\d+)",
                line,
            )
            if not m:
                continue
            port, name, pid = int(m.group(1)), m.group(2), int(m.group(3))
            key = ("TCP", port, pid)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"proto": "TCP", "port": port, "pid": pid, "state": "LISTENING",
                         "name": name, "cmdline": cmds.get(pid, ""), "local": ""})
        return rows

    lsof = _run(["lsof", "-i", "-P", "-n", "-s", "TCP:LISTEN"])
    for line in lsof.splitlines():
        m = re.match(r"^(\S+)\s+(\d+)\s+\S+\s+\S+\s+\S+\s+\S+\s+TCP\s+\S+:(\d+)\s+\(LISTEN\)", line)
        if not m:
            continue
        name, pid, port = m.group(1), int(m.group(2)), int(m.group(3))
        key = ("TCP", port, pid)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"proto": "TCP", "port": port, "pid": pid, "state": "LISTENING",
                     "name": name, "cmdline": cmds.get(pid, ""), "local": ""})
    return rows


# ── 種別判定（開発ランタイム） ───────────────────────────────────────────────────

# プロセス名（小文字）→ 種別キー。開発用途でよく使うランタイムを区別する。
_KIND_RULES = [
    ("node",   ("node.exe", "node")),
    ("bun",    ("bun.exe", "bun")),
    ("deno",   ("deno.exe", "deno")),
    ("python", ("python.exe", "pythonw.exe", "python3", "python")),
    ("dart",   ("dart.exe", "dart", "flutter")),
    ("go",     ("go.exe",)),
    ("java",   ("java.exe", "javaw.exe", "java")),
    ("php",    ("php.exe", "php")),
    ("ruby",   ("ruby.exe", "ruby")),
    ("dotnet", ("dotnet.exe", "dotnet")),
]

# 「開発用途と思われる」種別（UI で強調する対象）
DEV_KINDS = {"node", "bun", "deno", "python", "dart", "go", "java", "php", "ruby", "dotnet"}


def classify(name: str, cmdline: str = "") -> str:
    """プロセス名から種別キーを返す（不明なら 'other'）。"""
    n = (name or "").lower()
    for kind, needles in _KIND_RULES:
        if any(n == x or n.startswith(x) for x in needles):
            return kind
    return "other"


# ── Web サーバー判定（軽量 HTTP プローブ） ─────────────────────────────────────

def probe_http(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    """ポートが HTTP を喋るかを判定。GET / を送り応答が "HTTP/" で始まれば True。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n"
                      b"User-Agent: PortWho\r\nConnection: close\r\n\r\n")
            head = s.recv(16)
        return head[:5] == b"HTTP/"
    except OSError:
        return False


def probe_web_ports(ports_list, timeout: float = 0.4, workers: int = 24) -> set[int]:
    """ポート群を並列にプローブし、HTTP を喋ったポート番号の集合を返す。"""
    targets = sorted({int(p) for p in ports_list})
    if not targets:
        return set()
    web: set[int] = set()
    with ThreadPoolExecutor(max_workers=min(workers, len(targets))) as ex:
        futs = {ex.submit(probe_http, p, "127.0.0.1", timeout): p for p in targets}
        for f, p in futs.items():
            try:
                if f.result():
                    web.add(p)
            except Exception:
                pass
    return web


# ── 公開 API ────────────────────────────────────────────────────────────────────

def list_listening() -> list[dict]:
    """リッスン中のポート一覧を返す。

    各要素: {proto, port, pid, state, name, cmdline, local, kind}
    ポート昇順、同ポート内は proto 昇順。
    """
    rows = _listening_win() if IS_WIN else _listening_unix()
    for r in rows:
        r["kind"] = classify(r["name"], r["cmdline"])
    rows.sort(key=lambda r: (r["port"], r["proto"]))
    return rows


def kill_pid(pid: int) -> tuple[bool, str]:
    """PID をプロセスツリーごと強制終了。(成功, メッセージ) を返す。"""
    if IS_WIN:
        args = ["taskkill", "/PID", str(pid), "/T", "/F"]
    else:
        args = ["kill", "-9", str(pid)]
    try:
        cp = subprocess.run(
            args, capture_output=True, timeout=10,
            creationflags=_NO_WINDOW if IS_WIN else 0,
        )
    except Exception as e:
        return False, str(e)
    msg = _decode(cp.stdout or b"") or _decode(cp.stderr or b"")
    return cp.returncode == 0, msg.strip()


if __name__ == "__main__":
    # CLI スモーク: python ports.py [フィルタ]
    q = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    for r in list_listening():
        if q and q not in str(r["port"]) and q not in r["name"].lower() and q not in r["cmdline"].lower():
            continue
        print(f"{r['proto']:4} {r['port']:>6}  pid={r['pid']:<7} {r['name']:<24} {r['cmdline'][:80]}")
