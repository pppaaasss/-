#!/opt/bin/python3
"""One IPv4 marked-connection experiment; never declares TV-path equivalence.

Uses system DNS solely to locate GitHub for this experiment. The marked TCP
connection enters the existing merlinclash chain. Its exact temporary OUTPUT
rule is removed on normal exit, exceptions, SIGINT, SIGTERM and the time limit.
No probe configuration, playlist, cron entry or Clash setting is written.
"""

from __future__ import annotations

import os
import signal
import socket
import ssl
import subprocess
import sys


HOST = "raw.githubusercontent.com"
PATH = "/pppaaasss/-/master/tv-core.m3u"


def firewall(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["iptables", "-t", "nat", *args],
        capture_output=True, text=True, timeout=5,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "iptables command failed")
    return result


def check_connection() -> None:
    firewall("-S", "merlinclash")
    addresses = socket.getaddrinfo(HOST, 443, socket.AF_INET, socket.SOCK_STREAM)
    if not addresses:
        raise RuntimeError("GitHub IPv4 resolution returned no addresses")
    address = addresses[0][4]
    # Distinct from the firmware's existing 0xc0a8... subnet marks.
    mark = 0x49500000 | (os.getpid() & 0xFFFF)
    rule = (
        "-p", "tcp", "-d", address[0] + "/32",
        "-m", "mark", "--mark", f"{mark:#x}/0xffffffff",
        "-j", "merlinclash",
    )
    installed = False
    cleanup_failed = False
    connection = None
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        raw.settimeout(10)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, mark)
        print("SO_MARK: OK", flush=True)
        # Refuse to borrow or subsequently delete a pre-existing identical rule.
        if firewall("-C", "OUTPUT", *rule, check=False).returncode == 0:
            raise RuntimeError("An identical rule already exists; nothing changed")
        # Arm cleanup before insertion so an interruption cannot leave an
        # inserted rule untracked. A failed insertion is harmless to clean up.
        installed = True
        firewall("-I", "OUTPUT", "1", *rule)
        raw.connect(address)
        connection = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
        request = (
            f"GET {PATH} HTTP/1.1\r\nHost: {HOST}\r\n"
            "Range: bytes=0-127\r\nConnection: close\r\n"
            "User-Agent: AC86U-IPTV-Route-Check/1.0\r\n\r\n"
        )
        connection.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n" not in response and len(response) < 1024:
            part = connection.recv(min(256, 1024 - len(response)))
            if not part:
                break
            response.extend(part)
        status = bytes(response).split(b"\r\n", 1)[0].decode("ascii", "replace")
        fields = status.split()
        if len(fields) < 2 or fields[1] not in {"200", "206"}:
            raise RuntimeError("Unexpected HTTP response: " + status[:120])
        print("MARKED_HTTPS: " + status, flush=True)
    finally:
        # Give rollback its own short deadline even after the test alarm fired.
        signal.alarm(0)
        if connection is not None:
            connection.close()
        raw.close()
        if installed:
            try:
                removed = firewall("-D", "OUTPUT", *rule, check=False)
                if removed.returncode:
                    still_present = firewall("-C", "OUTPUT", *rule, check=False)
                    cleanup_failed = still_present.returncode != 1
            except Exception:
                cleanup_failed = True
            if cleanup_failed:
                print("CLEANUP FAILED. Remove only this rule:", file=sys.stderr)
                print("iptables -t nat -D OUTPUT " + " ".join(rule), file=sys.stderr)
            else:
                print("TEMP_RULE: REMOVED", flush=True)
        if cleanup_failed:
            raise RuntimeError("Temporary rule cleanup needs attention")


def interrupted(_signum: int, _frame: object) -> None:
    raise InterruptedError("Route check interrupted or exceeded 40 seconds")


def main() -> int:
    if os.geteuid() != 0:
        print("Run inside the router administrator SSH session", file=sys.stderr)
        return 2
    for signum in (signal.SIGALRM, signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, interrupted)
    signal.alarm(40)
    try:
        check_connection()
    except Exception as exc:
        print("CHECK_FAILED: " + str(exc), file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
    print("Diagnostic only: household DNS, IPv6 and ffprobe path are NOT yet verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
