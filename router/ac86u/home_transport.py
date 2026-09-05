"""Loopback HTTP/CONNECT adapter for the existing MerlinClash IPv4 rules.

DNS A/AAAA queries go explicitly to the LAN DNS server. IPv4 TCP sockets use
SO_MARK; IPv6 sockets remain direct, matching the observed empty IPv6 rules.
This module does NOT certify equivalence to a particular television policy.
Wire formats: RFC 1035 sections 4.1/4.2; HTTP CONNECT: RFC 9110 section 9.3.6.
"""
from __future__ import annotations

import contextlib
import errno
import http.server
import ipaddress
import os
import secrets
import selectors
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request


def exact(sock, count):
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            raise OSError("truncated DNS reply")
        chunks.extend(chunk)
    return bytes(chunks)


def dns_name(data, offset):
    labels, visited, end = [], set(), None
    while True:
        if offset in visited or offset >= len(data):
            raise ValueError("invalid DNS compression")
        visited.add(offset)
        size = data[offset]
        if size & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("truncated DNS pointer")
            if end is None:
                end = offset + 2
            offset = ((size & 63) << 8) | data[offset + 1]
            continue
        if size & 0xC0 or size > 63 or offset + 1 + size > len(data):
            raise ValueError("invalid DNS label")
        offset += 1
        if not size:
            return b".".join(labels).decode("ascii").lower(), end or offset
        labels.append(data[offset:offset + size])
        offset += size
        if sum(map(len, labels)) + len(labels) > 255:
            raise ValueError("DNS name too long")


def dns_reply(data, ident, host, kind):
    if len(data) < 12:
        raise ValueError("short DNS header")
    actual, flags, questions, answers, _, _ = struct.unpack("!6H", data[:12])
    if actual != ident or not flags & 0x8000 or flags & 0x7A00 or questions != 1:
        raise ValueError("unexpected DNS response")
    question, cursor = dns_name(data, 12)
    if question != host or data[cursor:cursor + 4] != struct.pack("!HH", kind, 1):
        raise ValueError("DNS question mismatch")
    if flags & 15:
        raise OSError("LAN DNS response code " + str(flags & 15))
    cursor += 4
    records = []
    for _ in range(answers):
        owner, cursor = dns_name(data, cursor)
        if cursor + 10 > len(data):
            raise ValueError("truncated DNS record")
        rtype, rclass, ttl, length = struct.unpack("!HHIH", data[cursor:cursor + 10])
        cursor += 10
        end = cursor + length
        if end > len(data):
            raise ValueError("truncated DNS data")
        value = None
        if rclass == 1 and rtype == 5:
            value, name_end = dns_name(data, cursor)
            if name_end != end:
                raise ValueError("invalid CNAME length")
        elif rclass == 1 and (rtype, length) in {(1, 4), (28, 16)}:
            value = str(ipaddress.ip_address(data[cursor:end]))
        records.append((owner, rtype, value, ttl))
        cursor = end
    allowed = {host}
    for _ in range(16):
        expanded = allowed | {v for n, t, v, _ in records if n in allowed and t == 5 and v}
        if expanded == allowed:
            break
        allowed = expanded
    selected = [(v, ttl) for n, t, v, ttl in records if n in allowed and t == kind and v]
    aliases_ttl = [ttl for n, t, v, ttl in records if n in allowed and t == 5 and v in allowed]
    return [v for v, _ in selected], min([ttl for _, ttl in selected] + aliases_ttl + [60])


class Landns:
    def __init__(self, server="192.168.50.1", port=53):
        self.server = str(ipaddress.ip_address(server))
        self.port, self.cache, self.lock = port, {}, threading.Lock()

    def query(self, host, kind):
        ident = secrets.randbelow(65536)
        labels = host.encode("idna").split(b".")
        if any(not x or len(x) > 63 for x in labels):
            raise ValueError("invalid DNS hostname")
        name = b"".join(bytes([len(x)]) + x for x in labels) + b"\0"
        if len(name) > 255:
            raise ValueError("DNS hostname too long")
        packet = struct.pack("!6H", ident, 0x100, 1, 0, 0, 0) + name + struct.pack("!HH", kind, 1)
        # Numeric server only: system resolver is never used for this query.
        family = socket.AF_INET6 if ":" in self.server else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect((self.server, self.port))
            sock.sendall(struct.pack("!H", len(packet)) + packet)
            length = struct.unpack("!H", exact(sock, 2))[0]
            return dns_reply(exact(sock, length), ident, host, kind)

    def resolve(self, host):
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            pass
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        with self.lock:
            cached = self.cache.get(host)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        values, ttl, errors = {}, 60, []
        for kind in (28, 1):
            try:
                values[kind], life = self.query(host, kind)
                ttl = min(ttl, life)
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
                values[kind] = []
        addresses = []
        for index in range(max(len(values[28]), len(values[1]))):
            for kind in (28, 1):
                if index < len(values[kind]):
                    addresses.append(values[kind][index])
        if not addresses:
            raise OSError("LAN DNS returned no addresses: " + "; ".join(errors))
        with self.lock:
            if len(self.cache) >= 256:
                self.cache.clear()
            self.cache[host] = time.monotonic() + ttl, addresses
        return addresses


def iptables(*args, check=True):
    result = subprocess.run(["iptables", "-t", "nat", *args], capture_output=True, text=True, timeout=5)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "iptables failed")
    return result


class Dialer:
    def __init__(self, resolver, mark, guard=lambda: None):
        self.resolver, self.mark, self.guard = resolver, mark, guard
        self.lock, self.active, self.closed = threading.Lock(), set(), False
        self.ipv4 = self.ipv6 = 0

    def close(self):
        with self.lock:
            self.closed = True
            for sock in tuple(self.active):
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            self.active.clear()

    def release(self, sock):
        with self.lock:
            self.active.discard(sock)
        sock.close()

    def connect(self, host, port):
        addresses = self.resolver.resolve(host)[:16]
        self.guard()
        pending, winner, failure = [], None, None
        deadline, next_start, index = time.monotonic() + 8, 0, 0
        with selectors.DefaultSelector() as selector:
            try:
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if index < len(addresses) and (now >= next_start or not pending):
                        address = addresses[index]
                        index += 1
                        family = socket.AF_INET6 if ":" in address else socket.AF_INET
                        sock = socket.socket(family, socket.SOCK_STREAM)
                        pending.append(sock)
                        with self.lock:
                            if self.closed:
                                raise OSError("transport closed")
                            self.active.add(sock)
                        if family == socket.AF_INET and self.mark is not None:
                            sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, self.mark)
                        sock.setblocking(False)
                        result = sock.connect_ex((address, port))
                        if result == 0:
                            winner = sock
                            break
                        if result not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                            failure = OSError(result, os.strerror(result))
                            self.release(sock)
                            pending.remove(sock)
                            continue
                        selector.register(sock, selectors.EVENT_WRITE)
                        next_start = now + 0.25
                    if not pending and index == len(addresses):
                        raise failure or OSError("no usable destination")
                    wait = min(0.25, max(0, deadline - time.monotonic()))
                    for key, _ in selector.select(wait):
                        sock = key.fileobj
                        selector.unregister(sock)
                        error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        if not error:
                            winner = sock
                            break
                        failure = OSError(error, os.strerror(error))
                        self.release(sock)
                        pending.remove(sock)
                    if winner is not None:
                        break
                if winner is None:
                    raise TimeoutError("destination connection timed out")
                winner.setblocking(True)
                winner.settimeout(10)
                with self.lock:
                    if winner.family == socket.AF_INET6:
                        self.ipv6 += 1
                    else:
                        self.ipv4 += 1
                return winner
            finally:
                for sock in pending:
                    if sock is not winner:
                        self.release(sock)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    rbufsize = 0

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        self.forward(True)

    def do_GET(self):
        self.forward(False)

    def do_HEAD(self):
        self.forward(False)

    def forward(self, tunnel):
        upstream, began = None, False
        self.close_connection = True
        try:
            url = urllib.parse.urlsplit("//" + self.path if tunnel else self.path)
            if not tunnel and url.scheme != "http":
                raise ValueError("HTTP absolute URI required")
            host, port = url.hostname, url.port or (443 if tunnel else 80)
            if not host or url.username or url.password or not 1 <= port <= 65535:
                raise ValueError("invalid proxy destination")
            if host in {"127.0.0.1", "localhost", "::1"} and port == self.server.server_port:
                raise ValueError("proxy loop")
            upstream = self.server.dialer.connect(host, port)
            if tunnel:
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                if self.headers.get("Transfer-Encoding") or int(self.headers.get("Content-Length", "0")):
                    raise ValueError("request body unsupported")
                path = urllib.parse.urlunsplit(("", "", url.path or "/", url.query, ""))
                headers = [f"{self.command} {path} HTTP/1.1", "Host: " + url.netloc]
                remove = {"host", "connection", "proxy-connection", "proxy-authorization", "keep-alive", "upgrade"}
                remove.update(x.strip().lower() for x in self.headers.get("Connection", "").split(","))
                headers.extend(f"{key}: {value}" for key, value in self.headers.items() if key.lower() not in remove)
                headers.append("Connection: close")
                upstream.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("iso-8859-1"))
            began = True
            with selectors.DefaultSelector() as selector:
                selector.register(self.connection, selectors.EVENT_READ, upstream)
                selector.register(upstream, selectors.EVENT_READ, self.connection)
                while not self.server.dialer.closed:
                    ready = selector.select(10)
                    if not ready:
                        break
                    for key, _ in ready:
                        data = key.fileobj.recv(65536)
                        if not data:
                            return
                        key.data.sendall(data)
        except (OSError, ValueError, RuntimeError):
            if not began:
                with contextlib.suppress(OSError):
                    self.send_response(502)
                    self.send_header("X-IPTV-Transport-Error", "1")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
        finally:
            if upstream is not None:
                self.server.dialer.release(upstream)


class ProxyServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, *_):
        pass


class AlwaysProxy(urllib.request.ProxyHandler):
    def proxy_open(self, req, proxy, type):
        # Explicitly avoid environment no_proxy bypass for health requests.
        original = req.type
        req.set_proxy(urllib.parse.urlsplit(proxy).netloc, "http")
        if original == type or original == "https":
            return None
        return self.parent.open(req, timeout=req.timeout)


class HomeTransport:
    def __init__(self, dns="192.168.50.1", *, firewall=True, resolver=None):
        self.mark = 0x49600000 | (os.getpid() & 65535)
        self.use_firewall, self.installed = firewall, False
        self.rule = ("-p", "tcp", "-m", "mark", "--mark", f"{self.mark:#x}/0xffffffff", "-j", "merlinclash")
        self.dialer = Dialer(resolver or Landns(dns), self.mark if firewall else None, self.guard)
        self.server = self.thread = None

    def guard(self):
        if self.use_firewall:
            iptables("-C", "OUTPUT", *self.rule)

    def __enter__(self):
        try:
            if self.use_firewall:
                iptables("-S", "merlinclash")
                if iptables("-C", "OUTPUT", *self.rule, check=False).returncode == 0:
                    raise RuntimeError("transport mark already in use")
                self.installed = True
                iptables("-I", "OUTPUT", "1", *self.rule)
            self.server = ProxyServer(("127.0.0.1", 0), ProxyHandler)
            self.server.dialer = self.dialer
            self.url = f"http://127.0.0.1:{self.server.server_port}"
            self.opener = urllib.request.build_opener(AlwaysProxy({"http": self.url, "https": self.url}))
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        self.dialer.close()
        if self.server:
            if self.thread:
                self.server.shutdown()
                self.thread.join(timeout=2)
            self.server.server_close()
        if self.installed:
            try:
                result = iptables("-D", "OUTPUT", *self.rule, check=False)
                clean = result.returncode == 0 or iptables("-C", "OUTPUT", *self.rule, check=False).returncode == 1
            except Exception:
                clean = False
            if not clean:
                raise RuntimeError("cleanup failed; run: iptables -t nat -D OUTPUT " + " ".join(self.rule))
            self.installed = False

    def __exit__(self, *_):
        self.close()

    def child_env(self):
        env = {k: v for k, v in os.environ.items() if k not in {"LD_LIBRARY_PATH", "LD_PRELOAD"}}
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            env[key] = self.url
        env["no_proxy"] = env["NO_PROXY"] = ""
        return env
