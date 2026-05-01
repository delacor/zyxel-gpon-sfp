#!/usr/bin/env python3
"""
Prometheus text exporter for Zyxel PMG3000-D20B GPON SFP temperatures.

SSH: admin / admin (see Readme.md), then Zyxel CLI: admin / 1234, then linuxshell,
then tail /proc/driver/optic/temperatures (same parsing as the README awk one-liner).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Optional, Tuple

try:
    import paramiko
except ImportError:
    print("Install paramiko: pip install paramiko", file=sys.stderr)
    sys.exit(1)


def parse_temperatures_line(line: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Match: tail -n1 /proc/driver/optic/temperatures | awk -F'[][]|,' ...
    awk $3/10 and $4/10 are SoC and OPTIC in °C (fields are 1-based).
    """
    parts = re.split(r"[][]|,", line)
    if len(parts) < 4:
        return None, None
    try:
        soc = float(parts[2]) / 10.0
        optic = float(parts[3]) / 10.0
        return soc, optic
    except (ValueError, IndexError):
        return None, None


def _strip_ansi(data: bytes) -> bytes:
    return re.sub(rb"\x1b\[[0-9;?]*[a-zA-Z]", b"", data)


def _read_until(
    chan: paramiko.Channel,
    until: List[bytes],
    timeout: float,
    buf: bytearray,
) -> Optional[bytes]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if chan.recv_ready():
            buf.extend(chan.recv(65536))
        plain = _strip_ansi(bytes(buf))
        for marker in until:
            if marker in plain:
                return marker
        time.sleep(0.02)
    return None


def scrape_temperatures(
    host: str,
    port: int,
    ssh_user: str,
    ssh_password: str,
    cli_user: str,
    cli_password: str,
    connect_timeout: float,
    io_timeout: float,
) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """
    Returns (soc_c, optic_c, error_message). Temperatures None on failure.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=ssh_user,
            password=ssh_password,
            timeout=connect_timeout,
            banner_timeout=connect_timeout,
            auth_timeout=connect_timeout,
            allow_agent=False,
            look_for_keys=False,
        )
    except Exception as e:
        return None, None, f"ssh_connect: {e}"

    chan = client.invoke_shell(term="vt100", width=200, height=48)
    chan.settimeout(0.1)
    buf = bytearray()

    try:
        # Zyxel CLI login (after SSH auth). Some firmware sessions are already
        # authenticated and show a prompt directly.
        cli_prompts = [b"ZYXEL#", b"Hal#", b"T&W#"]
        first_marker = _read_until(chan, [b"Login:", b"login:", *cli_prompts], io_timeout, buf)
        if first_marker is None:
            return None, None, "timeout waiting for CLI Login: or prompt"

        if first_marker in (b"Login:", b"login:"):
            chan.send(cli_user.encode("ascii") + b"\n")
            if _read_until(chan, [b"Password:", b"password:"], io_timeout, buf) is None:
                return None, None, "timeout waiting for CLI Password:"
            chan.send(cli_password.encode("ascii") + b"\n")
            if _read_until(chan, cli_prompts, io_timeout, buf) is None:
                snippet = bytes(buf)[-400:].decode("utf-8", errors="replace")
                return None, None, f"timeout waiting for CLI prompt (ZYXEL#/Hal#/T&W#); tail={snippet!r}"

        if _read_until(chan, cli_prompts, io_timeout, buf) is None:
            snippet = bytes(buf)[-400:].decode("utf-8", errors="replace")
            return None, None, f"timeout waiting for CLI prompt (ZYXEL#/Hal#/T&W#); tail={snippet!r}"

        # If stuck in HAL menu, exit to ZYXEL#
        if b"Hal#" in _strip_ansi(bytes(buf)):
            chan.send(b"exit\n")
            if _read_until(chan, [b"ZYXEL#"], io_timeout, buf) is None:
                return None, None, "timeout waiting for ZYXEL# after Hal# exit"

        chan.send(b"linuxshell\n")
        # BusyBox ash: admin@SFP:~# (avoid bare "# " — MOTD may contain '#')
        shell_markers = [b":~# ", b":/# ", b":~#", b":/#"]
        if _read_until(chan, shell_markers, io_timeout, buf) is None:
            tail = bytes(buf)[-500:].decode("utf-8", errors="replace")
            return None, None, f"timeout after linuxshell; tail={tail!r}"

        cmd = b"tail -n1 /proc/driver/optic/temperatures\n"
        chan.send(cmd)
        time.sleep(0.3)
        # Drain response until shell prompt again
        deadline = time.monotonic() + io_timeout
        while time.monotonic() < deadline:
            if chan.recv_ready():
                buf.extend(chan.recv(65536))
            plain = _strip_ansi(bytes(buf)).decode("utf-8", errors="replace")
            if re.search(r"[:~]#[ \t]*$", plain, re.MULTILINE):
                break
            time.sleep(0.05)
        else:
            return None, None, "timeout waiting for shell after tail command"

        plain = _strip_ansi(bytes(buf)).decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in plain.splitlines()]
        data_line = None
        needle = "/proc/driver/optic/temperatures"
        for i, ln in enumerate(lines):
            if needle in ln and i + 1 < len(lines):
                data_line = lines[i + 1]
                break
        if data_line is None:
            for ln in reversed(lines):
                if ln and not ln.endswith("#") and "tail" not in ln and "Login" not in ln:
                    if re.search(r"\d", ln):
                        data_line = ln
                        break
        if not data_line:
            return None, None, f"could not find temperature line in: {plain[-800:]!r}"

        soc, optic = parse_temperatures_line(data_line)
        if soc is None or optic is None:
            return None, None, f"parse failed for line: {data_line!r}"
        return soc, optic, None
    finally:
        try:
            chan.close()
        except Exception:
            pass
        client.close()


_lock = threading.Lock()
_last: dict = {}


def render_metrics(
    soc: Optional[float],
    optic: Optional[float],
    err: Optional[str],
    scrape_duration: float,
) -> bytes:
    lines = [
        "# HELP zyxel_sfp_soc_temperature_celsius SoC temperature (from /proc/driver/optic/temperatures).",
        "# TYPE zyxel_sfp_soc_temperature_celsius gauge",
    ]
    if soc is not None:
        lines.append(f"zyxel_sfp_soc_temperature_celsius {soc:.6f}")
    else:
        lines.append("zyxel_sfp_soc_temperature_celsius NaN")

    lines += [
        "# HELP zyxel_sfp_optic_temperature_celsius Optic / module temperature (same source).",
        "# TYPE zyxel_sfp_optic_temperature_celsius gauge",
    ]
    if optic is not None:
        lines.append(f"zyxel_sfp_optic_temperature_celsius {optic:.6f}")
    else:
        lines.append("zyxel_sfp_optic_temperature_celsius NaN")

    ok = 1.0 if err is None else 0.0
    lines += [
        "# HELP zyxel_sfp_temperature_scrape_success 1 if last scrape got valid temperatures.",
        "# TYPE zyxel_sfp_temperature_scrape_success gauge",
        f"zyxel_sfp_temperature_scrape_success {ok:.0f}",
        "# HELP zyxel_sfp_temperature_scrape_duration_seconds Time spent on SSH session for last scrape.",
        "# TYPE zyxel_sfp_temperature_scrape_duration_seconds gauge",
        f"zyxel_sfp_temperature_scrape_duration_seconds {scrape_duration:.6f}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description="Zyxel GPON SFP temperature Prometheus exporter")
    p.add_argument("--listen", default=os.environ.get("LISTEN", "127.0.0.1:9817"))
    p.add_argument("--host", default=os.environ.get("ZYXEL_SFP_HOST", "10.10.1.1"))
    p.add_argument("--ssh-port", type=int, default=int(os.environ.get("ZYXEL_SSH_PORT", "22")))
    p.add_argument("--ssh-user", default=os.environ.get("ZYXEL_SSH_USER", "admin"))
    p.add_argument("--ssh-password", default=os.environ.get("ZYXEL_SSH_PASSWORD", "admin"))
    p.add_argument("--cli-user", default=os.environ.get("ZYXEL_CLI_USER", "admin"))
    p.add_argument("--cli-password", default=os.environ.get("ZYXEL_CLI_PASSWORD", "1234"))
    p.add_argument("--connect-timeout", type=float, default=10.0)
    p.add_argument("--io-timeout", type=float, default=25.0)
    args = p.parse_args()

    host_port = args.listen.rsplit(":", 1)
    bind_host = host_port[0]
    bind_port = int(host_port[1]) if len(host_port) > 1 else 9817

    def do_scrape() -> Tuple[Optional[float], Optional[float], Optional[str], float]:
        t0 = time.monotonic()
        soc, optic, err = scrape_temperatures(
            args.host,
            args.ssh_port,
            args.ssh_user,
            args.ssh_password,
            args.cli_user,
            args.cli_password,
            args.connect_timeout,
            args.io_timeout,
        )
        dt = time.monotonic() - t0
        return soc, optic, err, dt

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *a) -> None:
            sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % a))

        def do_GET(self) -> None:
            if self.path not in ("/", "/metrics"):
                self.send_response(404)
                self.end_headers()
                return
            with _lock:
                soc, optic, err, dt = do_scrape()
                _last["soc"], _last["optic"], _last["err"], _last["dt"] = soc, optic, err, dt
                if err:
                    sys.stderr.write(f"sfp_temperature_exporter: scrape failed: {err}\n")
                body = render_metrics(soc, optic, err, dt)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Scraper disconnected before reading the full response.
                pass

    httpd = HTTPServer((bind_host, bind_port), Handler)
    print(
        f"Listening on http://{bind_host}:{bind_port}/metrics "
        f"(SFP {args.host}:{args.ssh_port}, SSH {args.ssh_user}/***, CLI {args.cli_user}/***)",
        file=sys.stderr,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
