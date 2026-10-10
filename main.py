import asyncio
import json
import os
import re
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
    "tcp_timeout": 3,
    "site": "https://api.ipify.org?format=json",
    "output_dir": "output",
}


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
        print("config is broken, using defaults")
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
        print("custom regex is broken, using normal parser instead")
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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"}

    async def _notify(name, ok, info):
        if on_source is None:
            return
        try:
            result = on_source(name, ok, info)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

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

    suppress = (ConnectionResetError, ConnectionAbortedError, ConnectionRefusedError, TimeoutError)
    try:
        suppress = suppress + (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError)
    except AttributeError:
        pass

    def _handle(loop, context):
        if isinstance(context.get("exception"), suppress):
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


async def _tcp_open(ip, port, timeout=3):
    try:
        conn = asyncio.open_connection(ip, int(port))
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        try:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
            except Exception:
                pass
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _http_check_shared(session, proxy_url, site, timeout):
    resp = await session.get(site, proxy=proxy_url, timeout=aiohttp.ClientTimeout(total=timeout))
    await resp.read()
    if resp.status >= 400:
        return False, "unknown"
    return True, _guess_anonymity(dict(resp.headers))


async def _socks_check(proxy_url, site, timeout):
    from aiohttp_socks import ProxyConnector

    connector = ProxyConnector.from_url(proxy_url)
    session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout))
    try:
        resp = await session.get(site)
        await resp.read()
        if resp.status >= 400:
            return False, "unknown"
        return True, _guess_anonymity(dict(resp.headers))
    finally:
        await session.close()


async def _try_proxy(proxy, site, timeout, http_session=None, tcp_timeout=3):
    if not await _tcp_open(proxy["ip"], proxy["port"], timeout=tcp_timeout):
        return False, "unknown"
    try:
        if proxy.get("protocol", "http") in ("socks4", "socks5"):
            return await _socks_check(_proxy_url(proxy), site, timeout)
        if http_session is None:
            timeout_cfg = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                return await _http_check_shared(session, _proxy_url(proxy), site, timeout)
        return await _http_check_shared(http_session, _proxy_url(proxy), site, timeout)
    except Exception:
        return False, "unknown"


async def try_proxy(proxy, site, timeout, sem, http_session=None, tcp_timeout=3):
    async with sem:
        start = time.perf_counter()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        alive, anon = await _try_proxy(proxy, site, timeout, http_session, tcp_timeout)
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


def _normalize_sites(site):
    if isinstance(site, (list, tuple)):
        sites = [s for s in site if s]
        return sites or ["https://api.ipify.org?format=json"]
    if isinstance(site, str) and "," in site:
        sites = [s.strip() for s in site.split(",") if s.strip()]
        return sites or [site]
    return [site]


async def validate_all(proxies, concurrency=500, timeout=10, site="https://api.ipify.org?format=json", on_result=None, tcp_timeout=3):
    import itertools

    if not proxies:
        return []
    sites = _normalize_sites(site)
    site_cycle = itertools.cycle(sites)
    sem = asyncio.Semaphore(max(1, concurrency))
    results = []

    connector = aiohttp.TCPConnector(limit=max(1, concurrency), limit_per_host=0, ttl_dns_cache=300)
    timeout_cfg = aiohttp.ClientTimeout(total=timeout, connect=min(5, timeout))
    async with aiohttp.ClientSession(connector=connector, timeout=timeout_cfg) as http_session:
        async def run_cb(r):
            if not on_result:
                return
            x = on_result(r)
            if asyncio.iscoroutine(x):
                await x

        async def do_one(p):
            s = next(site_cycle)
            r = await try_proxy(p, s, timeout, sem, http_session, tcp_timeout)
            results.append(r)
            await run_cb(r)

        async def check_many(chunk):
            for p in chunk:
                await do_one(p)

        workers_n = min(max(1, concurrency), len(proxies))
        chunks = [proxies[i::workers_n] for i in range(workers_n)]
        await asyncio.gather(*[asyncio.create_task(check_many(c)) for c in chunks])

    results.sort(key=lambda r: (not r["alive"], r["latency_ms"] if r["alive"] else 10**9))
    return results


STORE_FILE = BASE_DIR / "output" / "proxies.json"


def _store_path(cfg_or_path=None):
    if cfg_or_path is None:
        return STORE_FILE
    if isinstance(cfg_or_path, dict):
        return resolve(cfg_or_path["output_dir"]) / "proxies.json"
    return resolve(str(cfg_or_path)) / "proxies.json"


def load_store(path=None):
    p = Path(path) if path else STORE_FILE
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def save_store(rows, path=None):
    p = Path(path) if path else STORE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return p


def upsert_proxies(proxies, cfg_or_path=None):
    if not proxies:
        return 0
    path = _store_path(cfg_or_path)
    existing = load_store(path)
    index = {(r["ip"], r["port"], r.get("protocol", "http")): r for r in existing}

    for p in proxies:
        key = (p["ip"], p["port"], p.get("protocol", "http"))
        row = {
            "ip": p["ip"],
            "port": p["port"],
            "protocol": p.get("protocol", "http"),
            "alive": bool(p.get("alive", False)),
            "latency_ms": p.get("latency_ms", -1),
            "anonymity": p.get("anonymity", "unknown"),
            "last_checked": p.get("last_checked", ""),
            "source": p.get("source", ""),
        }
        index[key] = row

    merged = list(index.values())
    save_store(merged, path)
    return len(proxies)


def get_all(cfg_or_path=None):
    return load_store(_store_path(cfg_or_path))


def get_alive(protocol=None, cfg_or_path=None):
    rows = [r for r in get_all(cfg_or_path) if r.get("alive")]
    if protocol:
        rows = [r for r in rows if r.get("protocol") == protocol]
    rows.sort(key=lambda r: r.get("latency_ms", 10**9))
    return rows


def get_stats(cfg_or_path=None):
    rows = get_all(cfg_or_path)
    total = len(rows)
    alive = sum(1 for r in rows if r.get("alive"))
    by_protocol = {}
    for r in rows:
        proto = r.get("protocol", "http")
        bucket = by_protocol.setdefault(proto, {"alive": 0, "total": 0})
        bucket["total"] += 1
        if r.get("alive"):
            bucket["alive"] += 1
    latency_buckets = {"<500ms": 0, "500-1500ms": 0, "1500-3000ms": 0, ">3000ms": 0}
    for r in rows:
        if not r.get("alive"):
            continue
        lat = r.get("latency_ms", -1)
        if lat < 500:
            latency_buckets["<500ms"] += 1
        elif lat < 1500:
            latency_buckets["500-1500ms"] += 1
        elif lat < 3000:
            latency_buckets["1500-3000ms"] += 1
        else:
            latency_buckets[">3000ms"] += 1
    return {
        "total": total,
        "alive": alive,
        "dead": total - alive,
        "by_protocol": by_protocol,
        "latency_buckets": latency_buckets,
    }


def export_files(output_dir="output"):
    out = resolve(str(output_dir))
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for protocol in ("http", "socks4", "socks5"):
        rows = get_alive(protocol, output_dir)
        dest = out / f"{protocol}.txt"
        dest.write_text(
            "\n".join(f"{r['ip']}:{r['port']}" for r in rows) + ("\n" if rows else ""),
            encoding="utf-8",
        )
        written[protocol] = dest
    all_alive = get_alive(None, output_dir)
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
    rows = get_all(output_dir)
    rows.sort(
        key=lambda r: (
            r.get("protocol", ""),
            0 if r.get("alive") else 1,
            r.get("latency_ms", -1) if r.get("alive") else 10**9,
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
    alive = get_alive(None, cfg)
    written = export_files(cfg["output_dir"])
    print("saved:")
    for label, path in written.items():
        if label == "json":
            print(f"  {_rel(path)} ({len(alive)} proxies)")
        else:
            n = sum(1 for r in alive if r["protocol"] == label)
            print(f"  {_rel(path)} ({n} proxies)")
    dump = dump_db_text(cfg["output_dir"])
    print(f"  {_rel(dump)} ({len(get_all(cfg))} total rows)")


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
        return default


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
        t.add_row("4. site", str(cfg["site"]))
        t.add_row("5. output_dir", cfg["output_dir"])
        t.add_row("6. tcp_timeout (s)", str(cfg.get("tcp_timeout", 3)))
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
            elif choice == "6":
                cfg["tcp_timeout"] = int(Prompt.ask("tcp_timeout (seconds)", default=str(cfg.get("tcp_timeout", 3))))
            else:
                print("thats not a setting, try 1-6")
                continue
        except (EOFError, ValueError) as exc:
            print(f"nah that didnt work ({exc}), nothing changed")
            continue
        save_config(cfg)
        print("saved.")
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
            print("scrape cancelled, back to menu")
            return
    unchecked = [
        {"ip": p["ip"], "port": p["port"], "protocol": p.get("protocol", "http"),
         "alive": False, "latency_ms": -1, "anonymity": "unknown",
         "last_checked": "", "source": p.get("source", "")}
        for p in scraped
    ]
    upsert_proxies(unchecked, cfg)
    print(f"Scraped {len(scraped)} proxies from {len(sources)} sources.")
    for e in errors:
        print(f"! {e[:160]}")
    dump = dump_db_text(cfg["output_dir"])
    print(f"Full list saved to: {_rel(dump)} ({len(get_all(cfg))} rows)")
    print("Next: pick 2 to run them.")


async def validate_db_screen(console_, cfg):
    _quiet_loop()
    stored = get_all(cfg)
    if not stored:
        print("no proxies yet, go do option 1 first")
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
                tcp_timeout=cfg.get("tcp_timeout", 3),
            )
        except KeyboardInterrupt:
            print("validation cancelled, back to menu")
            return
    upsert_proxies(results, cfg)
    alive = sum(1 for r in results if r["alive"])
    print(f"{alive}/{len(results)} alive.")
    _save_text_files(cfg)


def stats_screen(cfg):
    stats = get_stats(cfg)
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
        print("no data yet, go scrape something first (option 1)")
    print("")

    lat = Table(title="Latency distribution (alive only)", box=box.ASCII)
    lat.add_column("Bucket")
    lat.add_column("Count")
    for bucket, count in stats["latency_buckets"].items():
        lat.add_row(bucket, str(count))
    console.print(lat)

    top = get_alive(None, cfg)[:5]
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
        print("cancelled, back to menu")


def _run_validate(cfg):
    try:
        asyncio.run(validate_db_screen(console, cfg))
    except KeyboardInterrupt:
        print("cancelled, back to menu")


def _do_choice(choice, cfg):
    if choice == "1":
        _run_scrape(cfg)
        return True
    if choice == "2":
        _run_validate(cfg)
        return True
    if choice == "3":
        stats_screen(cfg)
        return True
    if choice == "4":
        settings_screen(cfg)
        return True
    if _confirm("really exit?", default=True):
        print("bye!")
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
            print("bye!")
            break
        os.system("cls" if os.name == "nt" else "clear")
        try:
            keep_going = _do_choice(choice, cfg)
        except (KeyboardInterrupt, EOFError):
            print("cancelled, back to menu")
            keep_going = True
        except Exception as exc:
            print(f"oops something broke: {exc}")
            print("back to menu")
            keep_going = True
        if not keep_going:
            break
        _pause()


if __name__ == '__main__':
    run_menu()
