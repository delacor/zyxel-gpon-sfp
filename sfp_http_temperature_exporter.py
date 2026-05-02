#!/usr/bin/env python3
"""
Prometheus text exporter for Zyxel GPON SFP optic stats via HTTP.

Uses GET /cgi/get_gpon_info (same as zyxel_gpon_sfp.py / the Web UI).

The gpon_info.html UI uses evalJSON() on the response and keys temp, voltage,
current, tx_power, rx_power (see getGponInfoResult in firmware). We parse those
first, then fall back to labeled HTML regex and a loose dict walk.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

try:
    import demjson
except ImportError:
    demjson = None  # type: ignore


def _parse_float_unit(
    text: str,
    pattern: re.Pattern[str],
    conv: Callable[[str], float],
) -> Optional[float]:
    m = pattern.search(text)
    if not m:
        return None
    try:
        return conv(m.group(1))
    except (ValueError, TypeError):
        return None


def _coerce_float(val: Any) -> Optional[float]:
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        m = re.match(r"^([-+]?[\d.]+)", val.strip())
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


# Keys from gpon_info.html getGponInfoResult() — raw CGI values before UI adds units.
_FIRMWARE_GPON_KEYS = (
    ("temp", "temperature_c"),
    ("voltage", "voltage_v"),
    ("current", "current_ma"),
    ("tx_power", "tx_power_dbm"),
    ("rx_power", "rx_power_dbm"),
)


def _decode_json_root(body: str) -> Optional[dict]:
    raw = body.strip()
    if not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    if demjson is not None:
        try:
            obj = demjson.decode(raw)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _fill_from_firmware_gpon_keys(info: dict, out: Dict[str, Optional[float]]) -> None:
    for js_key, out_key in _FIRMWARE_GPON_KEYS:
        if js_key not in info:
            continue
        v = _coerce_float(info[js_key])
        if v is not None:
            out[out_key] = v


def _heuristic_walk_fill_missing(obj: dict, out: Dict[str, Optional[float]]) -> None:
    if not any(out[k] is None for k in out):
        return

    candidates: Dict[str, List[Tuple[float, str]]] = {
        "temperature_c": [],
        "voltage_v": [],
        "current_ma": [],
        "tx_power_dbm": [],
        "rx_power_dbm": [],
    }

    def consider_num(key: str, val: float, path: str) -> None:
        lk = key.lower()
        if re.search(r"temp", lk):
            candidates["temperature_c"].append((val, path))
        elif re.search(r"volt", lk) and "battery" not in lk:
            candidates["voltage_v"].append((val, path))
        elif re.search(r"curr|bias", lk):
            candidates["current_ma"].append((val, path))
        elif re.search(r"tx", lk) and re.search(r"pow", lk):
            candidates["tx_power_dbm"].append((val, path))
        elif re.search(r"rx", lk) and re.search(r"pow", lk):
            candidates["rx_power_dbm"].append((val, path))

    def consider_str(key: str, val: str, path: str) -> None:
        lk = key.lower()
        m = re.search(r"([\d.]+)\s*C\b", val, re.I)
        if m and re.search(r"temp", lk):
            candidates["temperature_c"].append((float(m.group(1)), path))
        m = re.search(r"([\d.]+)\s*V\b", val, re.I)
        if m and re.search(r"volt", lk):
            candidates["voltage_v"].append((float(m.group(1)), path))
        m = re.search(r"([\d.]+)\s*mA\b", val, re.I)
        if m and re.search(r"curr|bias", lk):
            candidates["current_ma"].append((float(m.group(1)), path))
        m = re.search(r"([-+]?[\d.]+)\s*dBm", val, re.I)
        if m:
            if re.search(r"tx", lk):
                candidates["tx_power_dbm"].append((float(m.group(1)), path))
            if re.search(r"rx", lk):
                candidates["rx_power_dbm"].append((float(m.group(1)), path))

    def walk(d: Any, prefix: str = "") -> None:
        if isinstance(d, dict):
            for k, v in d.items():
                path = f"{prefix}.{k}" if prefix else str(k)
                ks = str(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    consider_num(ks, float(v), path)
                elif isinstance(v, str):
                    consider_str(ks, v, path)
                elif isinstance(v, dict):
                    walk(v, path)
                elif isinstance(v, list):
                    for i, item in enumerate(v):
                        if isinstance(item, dict):
                            walk(item, f"{path}[{i}]")

    walk(obj)

    def pick(lst: List[Tuple[float, str]], prefer: str) -> Optional[float]:
        if not lst:
            return None
        for val, path in lst:
            if prefer in path.lower():
                return val
        return lst[0][0]

    picks = (
        ("temperature_c", "optic"),
        ("voltage_v", "optic"),
        ("current_ma", "optic"),
        ("tx_power_dbm", "tx"),
        ("rx_power_dbm", "rx"),
    )
    for out_key, prefer in picks:
        if out[out_key] is None:
            out[out_key] = pick(candidates[out_key], prefer)


def parse_gpon_info_body(body: str) -> Dict[str, Optional[float]]:
    """
    Extract optic metrics from raw HTTP body (JSON from CGI, or HTML labels).
    Returns keys: temperature_c, voltage_v, current_ma, tx_power_dbm, rx_power_dbm.
    """
    out: Dict[str, Optional[float]] = {
        "temperature_c": None,
        "voltage_v": None,
        "current_ma": None,
        "tx_power_dbm": None,
        "rx_power_dbm": None,
    }

    root = _decode_json_root(body)
    if root is not None:
        _fill_from_firmware_gpon_keys(root, out)

    if out["temperature_c"] is None:
        t = _parse_float_unit(
            body,
            re.compile(r"Temperature\s*:\s*([\d.]+)\s*C", re.I),
            float,
        )
        if t is not None:
            out["temperature_c"] = t

    if out["voltage_v"] is None:
        v = _parse_float_unit(
            body,
            re.compile(r"Voltage\s*:\s*([\d.]+)\s*V", re.I),
            float,
        )
        if v is not None:
            out["voltage_v"] = v

    if out["current_ma"] is None:
        c = _parse_float_unit(
            body,
            re.compile(r"Current\s*:\s*([\d.]+)\s*mA", re.I),
            float,
        )
        if c is not None:
            out["current_ma"] = c

    if out["tx_power_dbm"] is None:
        tx = _parse_float_unit(
            body,
            re.compile(r"TX\s*Power\s*:\s*([-+]?[\d.]+)\s*dBm", re.I),
            float,
        )
        if tx is not None:
            out["tx_power_dbm"] = tx

    if out["rx_power_dbm"] is None:
        rx = _parse_float_unit(
            body,
            re.compile(r"Rx\s*Power\s*:?\s*([-+]?[\d.]+)\s*dBm", re.I),
            float,
        )
        if rx is not None:
            out["rx_power_dbm"] = rx

    if root is not None and any(out[k] is None for k in out):
        _heuristic_walk_fill_missing(root, out)

    return out


def scrape_http(
    base_url: str,
    user: str,
    password: str,
    timeout: float,
    verify_tls: bool,
    extra_cookie: Optional[str],
) -> Tuple[Dict[str, Optional[float]], Optional[str]]:
    """
    Returns (metrics_dict, error). metrics values may be None per field.
    """
    base = base_url.rstrip("/")
    rand = random.random()
    url = f"{base}/cgi/get_gpon_info?rand={rand}"
    headers = {
        "Accept": "*/*",
        "Referer": f"{base}/gpon_info.html",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }
    if extra_cookie:
        headers["Cookie"] = extra_cookie
    try:
        r = requests.get(
            url,
            auth=(user, password),
            headers=headers,
            timeout=timeout,
            verify=verify_tls,
        )
    except requests.RequestException as e:
        return {}, str(e)
    if r.status_code != 200:
        return {}, f"HTTP {r.status_code}"
    metrics = parse_gpon_info_body(r.text)
    if metrics.get("temperature_c") is None:
        return metrics, "temperature not found in response (parse failed)"
    return metrics, None


_lock = threading.Lock()


def render_metrics(
    m: Dict[str, Optional[float]],
    err: Optional[str],
    duration: float,
) -> bytes:
    def gauge(name: str, help_text: str, val: Optional[float]) -> List[str]:
        lines = [
            f"# HELP {name} {help_text}",
            f"# TYPE {name} gauge",
        ]
        if val is not None and err is None:
            lines.append(f"{name} {val:.6f}")
        else:
            lines.append(f"{name} NaN")
        return lines

    lines: List[str] = []
    lines += gauge(
        "zyxel_sfp_optic_temperature_celsius",
        "Transceiver temperature from /cgi/get_gpon_info (HTTP).",
        m.get("temperature_c"),
    )
    lines += gauge(
        "zyxel_sfp_optic_voltage_volts",
        "Transceiver supply voltage.",
        m.get("voltage_v"),
    )
    lines += gauge(
        "zyxel_sfp_optic_current_milliamps",
        "Transceiver bias current.",
        m.get("current_ma"),
    )
    lines += gauge(
        "zyxel_sfp_optic_tx_power_dbm",
        "Transceiver TX optical power.",
        m.get("tx_power_dbm"),
    )
    lines += gauge(
        "zyxel_sfp_optic_rx_power_dbm",
        "Transceiver RX optical power.",
        m.get("rx_power_dbm"),
    )

    ok = 1.0 if err is None else 0.0
    lines += [
        "# HELP zyxel_sfp_http_scrape_success 1 if last scrape parsed temperature.",
        "# TYPE zyxel_sfp_http_scrape_success gauge",
        f"zyxel_sfp_http_scrape_success {ok:.0f}",
        "# HELP zyxel_sfp_http_scrape_duration_seconds Wall time for last HTTP scrape.",
        "# TYPE zyxel_sfp_http_scrape_duration_seconds gauge",
        f"zyxel_sfp_http_scrape_duration_seconds {duration:.6f}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Zyxel GPON SFP optic metrics Prometheus exporter (HTTP)"
    )
    p.add_argument("--listen", default=os.environ.get("LISTEN", "127.0.0.1:9817"))
    p.add_argument(
        "--base-url",
        default=os.environ.get("ZYXEL_SFP_BASE_URL", "http://10.10.1.1"),
        help="SFP web UI base URL, e.g. http://10.10.1.1",
    )
    p.add_argument("--user", default=os.environ.get("ZYXEL_HTTP_USER", "admin"))
    p.add_argument("--password", default=os.environ.get("ZYXEL_HTTP_PASSWORD", "1234"))
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument(
        "--insecure-tls",
        action="store_true",
        help="Skip TLS certificate verification (not needed for http://).",
    )
    p.add_argument(
        "--cookie",
        default=os.environ.get("ZYXEL_HTTP_COOKIE", ""),
        help="Optional raw Cookie header (e.g. user=admin|admin|guest|0|1|0). Empty omits.",
    )
    args = p.parse_args()

    host_port = args.listen.rsplit(":", 1)
    bind_host = host_port[0]
    bind_port = int(host_port[1]) if len(host_port) > 1 else 9817

    verify_tls = not args.insecure_tls
    cookie = args.cookie.strip() or None

    def do_scrape() -> Tuple[Dict[str, Optional[float]], Optional[str], float]:
        t0 = time.monotonic()
        metrics, err = scrape_http(
            args.base_url,
            args.user,
            args.password,
            args.timeout,
            verify_tls,
            cookie,
        )
        return metrics, err, time.monotonic() - t0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *a) -> None:
            sys.stderr.write(
                "%s - - [%s] %s\n"
                % (self.address_string(), self.log_date_time_string(), fmt % a)
            )

        def do_GET(self) -> None:
            if self.path not in ("/", "/metrics"):
                self.send_response(404)
                self.end_headers()
                return
            with _lock:
                metrics, err, dt = do_scrape()
                if err:
                    sys.stderr.write(f"sfp_temperature_exporter: {err}\n")
                body = render_metrics(metrics, err, dt)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = HTTPServer((bind_host, bind_port), Handler)
    print(
        f"Listening on http://{bind_host}:{bind_port}/metrics "
        f"-> GET {args.base_url.rstrip('/')}/cgi/get_gpon_info",
        file=sys.stderr,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
