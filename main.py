import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console(color_system=None)

BASE_DIR = Path(__file__).resolve().parent


DEFAULTS = {
    "sources_path": "sources.json",
    "concurrency": 500,
    "timeout": 10,
    "site": "https://api.ipify.org?format=json",
    "output_dir": "output",
}

DEFAULT_SOURCES = [
    {"name": "TheSpeedX HTTP", "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "format": "text", "protocol": "http"},
    {"name": "TheSpeedX SOCKS4", "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt", "format": "text", "protocol": "socks4"},
    {"name": "TheSpeedX SOCKS5", "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "format": "text", "protocol": "socks5"},
    {"name": "ShiftyTR HTTP", "url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt", "format": "text", "protocol": "http"},
    {"name": "ShiftyTR SOCKS4", "url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt", "format": "text", "protocol": "socks4"},
    {"name": "ShiftyTR SOCKS5", "url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt", "format": "text", "protocol": "socks5"},
    {"name": "monosans HTTP", "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "format": "text", "protocol": "http"},
    {"name": "monosans SOCKS4", "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt", "format": "text", "protocol": "socks4"},
    {"name": "monosans SOCKS5", "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "format": "text", "protocol": "socks5"},
    {"name": "clarketm HTTP", "url": "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt", "format": "text", "protocol": "http"},
    {"name": "openproxylist HTTPS", "url": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt", "format": "text", "protocol": "http"},
    {"name": "openproxylist SOCKS4", "url": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt", "format": "text", "protocol": "socks4"},
    {"name": "openproxylist SOCKS5", "url": "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt", "format": "text", "protocol": "socks5"},
    {"name": "FreeProxyList.net", "url": "https://free-proxy-list.net/", "format": "html", "protocol": "http"},
    {"name": "Spys.one", "url": "https://spys.one/en/free-proxy-list/", "format": "html", "protocol": "http"},
]


def config_path():
    return BASE_DIR / "config.json"


def load_config(path=None):
    cfg = dict(DEFAULTS)
    cfg_file = Path(path) if path else config_path()
    if not cfg_file.exists():
        save_config(cfg, cfg_file)
        return cfg
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("config is broken lol using defaults")
        return cfg
    for k in data:
        if k in cfg:
            cfg[k] = data[k]
    return cfg


def save_config(cfg, path=None):
    cfg_file = Path(path) if path else config_path()
    cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg_file


def resolve(path_str):
    p = Path(path_str)
    return p if p.is_absolute() else BASE_DIR / p


def load_sources(cfg):
    src_file = resolve(cfg["sources_path"])
    if not src_file.exists():
        src_file.write_text(json.dumps(DEFAULT_SOURCES, indent=2), encoding="utf-8")
        return list(DEFAULT_SOURCES)
    return json.loads(src_file.read_text(encoding="utf-8"))


PROXY_RE = re.compile(
    r"(?:(?P<scheme>https?|socks4|socks5)://)?"
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3})"
    r":(?P<port>\d{2,5})",
    re.IGNORECASE,
)


def _valid_ip(ip):
    try:
        return all(0 <= int(part) <= 255 for part in ip.split("."))
    except ValueError:
        return False


def _parse_match(match, default_protocol):
    ip = match.group("ip")
    port = int(match.group("port"))
    scheme = (match.group("scheme") or default_protocol).lower()
    if scheme == "https":
        scheme = "http"
    if scheme not in ("http", "socks4", "socks5"):
        scheme = default_protocol
    if not (1 <= port <= 65535) or not _valid_ip(ip):
        return None
    return {"ip": ip, "port": port, "protocol": scheme}


def extract_proxies(text, default_protocol="http"):
    seen = set()
    found = []
    for match in PROXY_RE.finditer(text or ""):
        proxy = _parse_match(match, default_protocol)
        if proxy is None:
            continue
        key = (proxy["ip"], proxy["port"], proxy["protocol"])
        if key in seen:
            continue
        seen.add(key)
        found.append(proxy)
    return found


def parse_text(content, default_protocol="http"):
    return extract_proxies(content, default_protocol)


def parse_html(content, default_protocol="http"):
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(content, "html.parser")
        text = soup.get_text(separator="\n")
    except ImportError:
        text = content
    return extract_proxies(text, default_protocol)


def parse_json(content, default_protocol="http"):
    try:
        data = json.loads(content)
        return extract_proxies(json.dumps(data), default_protocol)
    except (json.JSONDecodeError, TypeError):
        return extract_proxies(content, default_protocol)


def _custom_parse(content, pattern, default_protocol):
    try:
        custom = re.compile(pattern, re.IGNORECASE)
    except re.error:
        print("custom regex is broken using normal parser instead")
        return None
    seen = set()
    out = []
    for m in custom.finditer(content or ""):
        d = m.groupdict()
        try:
            port = int(d.get("port", 0))
        except (ValueError, TypeError):
            continue
        ip = d.get("ip", "")
        scheme = (d.get("scheme") or default_protocol).lower()
        if scheme == "https":
            scheme = "http"
        key = (ip, port, scheme)
        if not _valid_ip(ip) or not (1 <= port <= 65535) or key in seen:
            continue
        seen.add(key)
        out.append({"ip": ip, "port": port, "protocol": scheme})
    return out


def parse_source(content, format="text", default_protocol="http", pattern=None):
    if pattern:
        custom = _custom_parse(content, pattern, default_protocol)
        if custom is not None:
            return custom

    fmt = (format or "text").lower()
    if fmt == "html":
        return parse_html(content, default_protocol)
    if fmt == "json":
        return parse_json(content, default_protocol)
    return parse_text(content, default_protocol)


async def fetch_one(session, source, timeout=15):
    name = source.get("name", source.get("url", "unknown"))
    url = source["url"]
    fmt = source.get("format", "text")
    default_protocol = source.get("protocol", "http")
    pattern = source.get("regex")
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        resp.raise_for_status()
        content = await resp.text()
    proxies = parse_source(content, fmt, default_protocol, pattern)
    for p in proxies:
        p["source"] = name
    return proxies


async def fetch_all(sources, timeout=15, on_source=None):
    proxies = []
    errors = []
    headers = {"User-Agent": "Mozilla/5.0 (just a kid learning python)"}

    async def _notify(name, ok, info):
        if on_source is None:
            return
        result = on_source(name, ok, info)
        if asyncio.iscoroutine(result):
            await result

    async with aiohttp.ClientSession(headers=headers) as session:

        async def _one(source):
            name = source.get("name", source.get("url", "?"))
            try:
                found = await fetch_one(session, source, timeout)
                await _notify(name, True, f"{len(found)} proxies")
                return found
            except Exception as exc:
                msg = f"{name}: {type(exc).__name__}: {exc}"
                errors.append(msg)
                await _notify(name, False, str(exc)[:120])
                return []

        results = await asyncio.gather(*[_one(s) for s in sources])

    seen = set()
    for batch in results:
        for p in batch:
            key = (p["ip"], p["port"], p["protocol"])
            if key not in seen:
                seen.add(key)
                proxies.append(p)
    return proxies, errors


_TRANSPARENT_MARKERS = ("via", "x-forwarded-for", "proxy-connection", "forwarded")


def _quiet_loop():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    prev = loop.get_exception_handler()

    def _handle(loop, context):
        if isinstance(context.get("exception"), ConnectionResetError):
            return
        if prev is not None:
            prev(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handle)


def _guess_anonymity(headers):
    lowered = {k.lower() for k in headers}
    if any(m in lowered for m in _TRANSPARENT_MARKERS):
        return "transparent"
    return "anonymous"


def _proxy_url(proxy):
    return f"{proxy['protocol']}://{proxy['ip']}:{proxy['port']}"


async def _try_proxy(proxy, site, timeout):
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    socks = proxy.get("protocol", "http") in ("socks4", "socks5")
    if socks:
        from aiohttp_socks import ProxyConnector

        connector = ProxyConnector.from_url(_proxy_url(proxy))
        session = aiohttp.ClientSession(connector=connector, timeout=timeout_cfg)
    else:
        session = aiohttp.ClientSession(timeout=timeout_cfg)
    try:
        if socks:
            resp = await session.get(site)
        else:
            resp = await session.get(site, proxy=_proxy_url(proxy))
        await resp.read()
        if resp.status >= 400:
            return False, "unknown"
        return True, _guess_anonymity(dict(resp.headers))
    except Exception:
        return False, "unknown"
    finally:
        await session.close()


async def try_proxy(proxy, site, timeout, sem):
    async with sem:
        start = time.perf_counter()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        alive, anon = await _try_proxy(proxy, site, timeout)
        ms = int((time.perf_counter() - start) * 1000) if alive else -1
        return {
            "ip": proxy["ip"],
            "port": proxy["port"],
            "protocol": proxy.get("protocol", "http"),
            "alive": alive,
            "latency_ms": ms,
            "anonymity": anon,
            "last_checked": now,
            "source": proxy.get("source", ""),
        }


async def validate_all(proxies, concurrency=500, timeout=10, site="https://api.ipify.org?format=json", on_result=None):
    sem = asyncio.Semaphore(max(1, concurrency))
    results = []

    async def _one(proxy):
        res = await try_proxy(proxy, site, timeout, sem)
        results.append(res)
        if on_result is not None:
            out = on_result(res)
            if asyncio.iscoroutine(out):
                await out
        return res

    await asyncio.gather(*[_one(p) for p in proxies])
    results.sort(key=lambda r: (not r["alive"], r["latency_ms"] if r["alive"] else 10**9))
    return results


SCHEMA = """
CREATE TABLE IF NOT EXISTS proxies (
    ip TEXT NOT NULL,
    port INTEGER NOT NULL,
    protocol TEXT NOT NULL DEFAULT 'http',
    alive INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT -1,
    anonymity TEXT NOT NULL DEFAULT 'unknown',
    last_checked TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ip, port, protocol)
);
"""

DB_FILE = BASE_DIR / "proxies.db"


def get_db(path=None):
    db = sqlite3.connect(str(path or DB_FILE))
    db.execute(SCHEMA)
    db.commit()
    return db


def upsert_proxies(proxies, path=None):
    if not proxies:
        return 0
    db = get_db(path)
    rows = [
        (
            p["ip"],
            p["port"],
            p.get("protocol", "http"),
            1 if p.get("alive") else 0,
            p.get("latency_ms", -1),
            p.get("anonymity", "unknown"),
            p.get("last_checked", ""),
            p.get("source", ""),
        )
        for p in proxies
    ]
    with db:
        db.executemany("INSERT OR REPLACE INTO proxies (ip, port, protocol, alive, latency_ms, anonymity, last_checked, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    db.close()
    return len(rows)


def get_alive(protocol=None, path=None):
    db = get_db(path)
    db.row_factory = sqlite3.Row
    if protocol:
        rows = db.execute(
            "SELECT * FROM proxies WHERE alive=1 AND protocol=? ORDER BY latency_ms",
            (protocol,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM proxies WHERE alive=1 ORDER BY latency_ms"
        ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_all(path=None):
    db = get_db(path)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM proxies").fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_stats(path=None):
    db = get_db(path)
    total = db.execute("SELECT COUNT(*) FROM proxies").fetchone()[0]
    alive = db.execute("SELECT COUNT(*) FROM proxies WHERE alive=1").fetchone()[0]
    by_protocol = {}
    for protocol, alive_n, total_n in db.execute(
        "SELECT protocol, SUM(alive), COUNT(*) FROM proxies GROUP BY protocol"
    ).fetchall():
        by_protocol[protocol] = {"alive": alive_n or 0, "total": total_n}
    buckets = {"<500ms": 0, "500-1500ms": 0, "1500-3000ms": 0, ">3000ms": 0}
    for (lat,) in db.execute("SELECT latency_ms FROM proxies WHERE alive=1").fetchall():
        if lat < 500:
            buckets["<500ms"] += 1
        elif lat < 1500:
            buckets["500-1500ms"] += 1
        elif lat < 3000:
            buckets["1500-3000ms"] += 1
        else:
            buckets[">3000ms"] += 1
    db.close()
    return {
        "total": total,
        "alive": alive,
        "dead": total - alive,
        "by_protocol": by_protocol,
        "latency_buckets": buckets,
    }


def export_files(output_dir="output"):
    out = resolve(str(output_dir))
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for protocol in ("http", "socks4", "socks5"):
        rows = get_alive(protocol)
        dest = out / f"{protocol}.txt"
        dest.write_text(
            "\n".join(f"{r['ip']}:{r['port']}" for r in rows) + ("\n" if rows else ""),
            encoding="utf-8",
        )
        written[protocol] = dest
    all_alive = get_alive()
    dest_json = out / "proxies.json"
    dest_json.write_text(json.dumps(all_alive, indent=2), encoding="utf-8")
    written["json"] = dest_json
    return written


def _rel(path):
    try:
        return str(Path(path).relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def dump_db_text(output_dir="output"):
    out = resolve(str(output_dir))
    out.mkdir(parents=True, exist_ok=True)
    rows = get_all()
    rows.sort(
        key=lambda r: (
            r.get("protocol", ""),
            0 if r.get("alive") else 1,
            r.get("latency_ms", -1),
        )
    )
    dest = out / "all_proxies.txt"
    lines = ["# ip:port protocol status latency anonymity source"]
    for r in rows:
        status = "alive" if r.get("alive") else "dead"
        line = (
            f"{r['ip']}:{r['port']} {r.get('protocol', 'http')} "
            f"{status} {r.get('latency_ms', -1)}ms "
            f"{r.get('anonymity', 'unknown')} {r.get('source', '')}".rstrip()
        )
        lines.append(line)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def _save_text_files(cfg):
    alive = get_alive()
    written = export_files(cfg["output_dir"])
    print("Saved:")
    for label, path in written.items():
        if label == "json":
            print(f"  {_rel(path)} ({len(alive)} proxies)")
        else:
            n = sum(1 for r in alive if r["protocol"] == label)
            print(f"  {_rel(path)} ({n} proxies)")
    dump = dump_db_text(cfg["output_dir"])
    print(f"  {_rel(dump)} ({len(get_all())} total rows in DB)")


def show_banner():
    print("")
    print("======================================")
    print("  Proxy Scraper & Validator")
    print("             by me :D")
    print("======================================")
    print("")


def show_menu():
    print("Main menu:")
    print("  1. Scrape proxies")
    print("  2. Validate proxies")
    print("  3. View stats")
    print("  4. Settings")
    print("  5. Exit")
    print("")
    return Prompt.ask("Pick an option", choices=[str(i) for i in range(1, 6)], show_choices=False)


def _confirm(message, default=True):
    try:
        return Confirm.ask(message, default=default)
    except EOFError:
        return True if "exit" in message.lower() or "menu" in message.lower() else default


def _pause():
    try:
        input("Press Enter to go back...")
    except EOFError:
        pass


def settings_screen(cfg):
    def _table():
        t = Table(title="Settings", box=box.ASCII)
        t.add_column("Key")
        t.add_column("Value")
        t.add_row("1. sources_path", cfg["sources_path"])
        t.add_row("2. concurrency", str(cfg["concurrency"]))
        t.add_row("3. timeout (s)", str(cfg["timeout"]))
        t.add_row("4. site", cfg["site"])
        t.add_row("5. output_dir", cfg["output_dir"])
        return t

    console.print(Panel(_table(), box=box.ASCII))
    print("Enter a number to edit it, or press Enter to go back.")
    while True:
        try:
            choice = Prompt.ask("Edit which setting?", default="").strip()
        except EOFError:
            break
        if not choice:
            break
        try:
            if choice == "1":
                cfg["sources_path"] = Prompt.ask("sources_path", default=cfg["sources_path"])
            elif choice == "2":
                cfg["concurrency"] = int(Prompt.ask("concurrency", default=str(cfg["concurrency"])))
            elif choice == "3":
                cfg["timeout"] = int(Prompt.ask("timeout (seconds)", default=str(cfg["timeout"])))
            elif choice == "4":
                cfg["site"] = Prompt.ask("site", default=cfg["site"])
            elif choice == "5":
                cfg["output_dir"] = Prompt.ask("output_dir", default=cfg["output_dir"])
            else:
                print("thats not a setting lol, try 1-5")
                continue
        except (EOFError, ValueError) as exc:
            print(f"nah that didnt work ({exc}), nothing changed")
            continue
        save_config(cfg)
        print("Saved.")
        console.print(Panel(_table(), box=box.ASCII))
    return cfg


async def scrape_only_screen(console_, cfg):
    _quiet_loop()
    try:
        sources = load_sources(cfg)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cant load sources file: {exc}")
        return
    with console_.status("Scraping sources...") as status:
        async def on_source(name, ok, info):
            status.update(f"{name}: {info}")

        try:
            scraped, errors = await fetch_all(sources, timeout=cfg["timeout"], on_source=on_source)
        except KeyboardInterrupt:
            print("Scrape cancelled, back to menu.")
            return
    unchecked = [
        {"ip": p["ip"], "port": p["port"], "protocol": p.get("protocol", "http"),
         "alive": False, "latency_ms": -1, "anonymity": "unknown",
         "last_checked": "", "source": p.get("source", "")}
        for p in scraped
    ]
    upsert_proxies(unchecked)
    print(f"Scraped {len(scraped)} proxies from {len(sources)} sources.")
    for e in errors:
        print(f"! {e[:160]}")
    dump = dump_db_text(cfg["output_dir"])
    print(f"Full DB saved to: {_rel(dump)} ({len(get_all())} rows)")
    print("Next: pick 2 to run them.")


async def validate_db_screen(console_, cfg):
    _quiet_loop()
    stored = get_all()
    if not stored:
        print("DB is empty. Use option 1 (Scrape) first.")
        return
    print(f"Running {len(stored)} saved proxies... (go grab a snack, this takes a while)")
    with Progress() as progress:
        job = progress.add_task("cooking", total=len(stored))

        async def on_result(res):
            progress.advance(job)

        try:
            results = await validate_all(
                stored, concurrency=cfg["concurrency"], timeout=cfg["timeout"],
                site=cfg["site"], on_result=on_result,
            )
        except KeyboardInterrupt:
            print("Validation cancelled, back to menu.")
            return
    upsert_proxies(results)
    alive = sum(1 for r in results if r["alive"])
    print(f"{alive}/{len(results)} alive.")
    _save_text_files(cfg)


def stats_screen():
    stats = get_stats()
    print("")
    print("Overview:")
    print(f"  Total: {stats['total']}   Alive: {stats['alive']}   Dead: {stats['dead']}")
    print("")

    if stats["by_protocol"]:
        proto = Table(title="By protocol", box=box.ASCII)
        proto.add_column("Protocol")
        proto.add_column("Alive")
        proto.add_column("Total")
        for name, vals in stats["by_protocol"].items():
            proto.add_row(name, str(vals["alive"]), str(vals["total"]))
        console.print(proto)
    else:
        print("No data yet. go scrape something first (option 1)")
    print("")

    lat = Table(title="Latency distribution (alive only)", box=box.ASCII)
    lat.add_column("Bucket")
    lat.add_column("Count")
    for bucket, count in stats["latency_buckets"].items():
        lat.add_row(bucket, str(count))
    console.print(lat)

    top = get_alive()[:5]
    if top:
        print("")
        tab = Table(title="Top 5 quickest", box=box.ASCII)
        tab.add_column("Proxy")
        tab.add_column("Proto")
        tab.add_column("Latency")
        for r in top:
            tab.add_row(f"{r['ip']}:{r['port']}", r["protocol"], f"{r['latency_ms']} ms")
        console.print(tab)


def _run_scrape(cfg):
    try:
        asyncio.run(scrape_only_screen(console, cfg))
    except KeyboardInterrupt:
        print("Cancelled - back to menu.")


def _run_validate(cfg):
    try:
        asyncio.run(validate_db_screen(console, cfg))
    except KeyboardInterrupt:
        print("Cancelled - back to menu.")


def _do_choice(choice, cfg):
    if choice == "1":
        _run_scrape(cfg)
        return True
    if choice == "2":
        _run_validate(cfg)
        return True
    if choice == "3":
        stats_screen()
        return True
    if choice == "4":
        settings_screen(cfg)
        return True
    if _confirm("Really exit?", default=True):
        print("Bye!")
        return False
    return True


def run_menu():
    cfg = load_config()
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        show_banner()
        try:
            choice = show_menu()
        except (KeyboardInterrupt, EOFError):
            print("")
            print("Bye!")
            break
        os.system("cls" if os.name == "nt" else "clear")
        try:
            keep_going = _do_choice(choice, cfg)
        except (KeyboardInterrupt, EOFError):
            print("Cancelled - back to menu.")
            keep_going = True
        except Exception as exc:
            print(f"Something went wrong: {exc}")
            print("Back to menu.")
            keep_going = True
        if not keep_going:
            break
        _pause()


run_menu()
