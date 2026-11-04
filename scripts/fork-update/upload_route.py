"""One alternate TLS route to GitHub's release upload host."""
import ipaddress
import json
from pathlib import Path
import subprocess
from urllib.parse import urlencode

HOST = "uploads.github.com"
CURL = "/opt/homebrew/opt/curl/bin/curl"


def curl(*args, timeout, headers=None):
    # Ignore curlrc, proxies, tracing and TLS environment overrides. Never follow redirects.
    binary = CURL if Path(CURL).is_file() else "/usr/bin/curl"
    return subprocess.run([binary, "--disable", "--silent", "--http1.1", "--proto", "=https",
                           "--connect-timeout", "15", "--max-time", str(timeout), *args],
                          input=headers, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          env={}, timeout=timeout + 5)


def public_json(url):
    result = curl("--fail", "--max-filesize", "2097152", url, timeout=20)
    result.check_returncode()
    return json.loads(result.stdout)


def normal_addresses():
    # Exclude all IPv4 addresses on the Mac's ordinary resolver path.
    lookup = subprocess.run(["/usr/bin/dscacheutil", "-q", "host", "-a", "name", HOST],
                            capture_output=True, text=True, timeout=20, check=True)
    local = {line.split()[-1] for line in lookup.stdout.splitlines()
             if line.startswith(("ip_address:", "ipv6_address:"))}
    if not local:
        raise ValueError("Cannot verify the normal upload route")
    return local


def alternate_address():
    local = normal_addresses()
    dns = public_json("https://dns.google/resolve?name=uploads.github.com&type=A&edns_client_subnet=8.8.8.0/24")
    if dns["Status"] != 0:
        raise ValueError("Alternate upload lookup failed")
    rows = dns["Answer"]
    origin = HOST
    for _ in range(8):
        aliases = [r["data"].lower().rstrip(".") for r in rows
                   if r["type"] == 5 and r["name"].lower().rstrip(".") == origin]
        if not aliases:
            break
        if len(aliases) != 1 or not aliases[0].endswith(".githubusercontent.com"):
            raise ValueError("Unexpected upload alias")
        origin = aliases[0]
    else:
        raise ValueError("Unexpected upload alias chain")
    ranges = [ipaddress.ip_network(net) for net in public_json("https://api.github.com/meta")["web"]]
    for row in rows:
        if row["type"] == 1 and row["name"].lower().rstrip(".") == origin:
            addr = ipaddress.IPv4Address(row["data"])
            if addr.is_global and str(addr) not in local and any(addr in net for net in ranges):
                return str(addr)
    raise ValueError("No distinct verified GitHub upload address")


def upload(repo, release_id, path, address, token):
    if not token or not token.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
        raise ValueError("Invalid GitHub credential")
    endpoint = f"https://{HOST}/repos/{repo}/releases/{release_id}/assets?" + urlencode({"name": path.name})
    try:
        result = curl("--resolve", f"{HOST}:443:{address}", "--request", "POST",
                      "--header", "@-", "--header", "Content-Type: application/octet-stream",
                      "--max-filesize", "1048576", "--upload-file", str(path), "--write-out", "\\n%{http_code}", endpoint,
                      headers=f"Authorization: Bearer {token}\n".encode(), timeout=7200)
    except subprocess.TimeoutExpired:
        return None
    body, _, status = result.stdout.rpartition(b"\n")
    if result.returncode or status != b"201":
        return None  # The server might have committed before the connection failed.
    if not body.strip():
        return None
    try:
        receipt = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        # A truncated reply is inconclusive. The caller must read the asset back and check its digest.
        return None
    if not isinstance(receipt, dict):
        raise ValueError("Invalid GitHub upload receipt")
    return receipt
