"""URL validation to prevent SSRF attacks.

Single source of truth for blocked networks and hostnames used by both
property list resolution and webhook URL validation.
"""

import ipaddress
import socket
from urllib.parse import urlparse


def sanitize_for_log(value: object) -> str:
    """Return a log-safe string representation of *value*.

    Strips newline and carriage-return characters so that a user-supplied
    string cannot inject fake log lines (CWE-117 / CodeQL Log Injection).
    The result is always a plain ``str`` — no repr quoting — so it reads
    naturally in log output.
    """
    return str(value).replace("\r", " ").replace("\n", " ")


# Blocked IP ranges (RFC 1918 private networks, loopback, link-local)
BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# Blocked hostnames (cloud metadata services, localhost aliases, Docker-internal hostnames)
BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "169.254.169.254",
    "metadata",
    "instance-data",
    # Docker-internal hostnames that resolve to private/loopback IPs and
    # are not guaranteed to be caught by DNS resolution in all environments
    "host.docker.internal",
    "gateway.docker.internal",
    "docker.host.internal",
}


def is_local_host(host: str) -> bool:
    """True if *host* (a hostname, optionally with ``:port``) is a local dev host.

    The single local-host predicate in the codebase.  Two call sites need it and
    they must not disagree:

    - ``src/app.py`` (``_create_dynamic_agent_card``) — picks ``http`` vs
      ``https`` for the advertised A2A agent-card URL when no
      ``X-Forwarded-Proto`` is present.
    - ``src/services/tmp_provider_sync.py`` (``_resolve_seller_agent_url``) —
      skips a tenant ``virtual_host`` that cannot produce a valid https
      ``seller_agent.agent_url``.

    Both answer the same question ("can this host serve https?"), so they share
    one implementation; before this existed they forked on ``*.localhost``
    (``tenant.localhost`` was public to one and local to the other, #1197 review).

    Uses exact equality / suffix checks rather than substring tests: a substring
    test (``"localhost" in host``) misclassifies ``my-localhost-mirror.example.com``
    as local, and ``host.startswith("127.0.0.1")`` misclassifies
    ``127.0.0.1.evil.com`` as loopback.

    Note this is a *deployment-shape* predicate, not a security boundary — SSRF
    checks live in :func:`check_url_ssrf`, which resolves DNS and rejects the
    whole loopback/private range rather than these three literal forms.
    """
    hostname = host.split(":", 1)[0].lower()
    return hostname == "localhost" or hostname.endswith(".localhost") or hostname == "127.0.0.1"


def check_url_ssrf(url: str, *, require_https: bool = False) -> tuple[bool, str]:
    """Check a URL for SSRF safety.

    Validates that the URL does not target private/internal networks
    or cloud metadata services.

    Args:
        url: The URL to validate.
        require_https: If True, reject non-HTTPS schemes. If False,
            allow both HTTP and HTTPS.

    Returns:
        (is_safe, error_message) -- is_safe is True if the URL is safe,
        error_message describes the problem if not.
    """
    try:
        parsed = urlparse(url)

        if require_https:
            if parsed.scheme != "https":
                return False, f"URL must use HTTPS scheme, got '{parsed.scheme}'"
        elif parsed.scheme not in ("http", "https"):
            return False, "URL must use http or https protocol"

        hostname = parsed.hostname
        if not hostname:
            return False, "URL must have a valid hostname"

        if hostname.lower() in BLOCKED_HOSTNAMES:
            return False, f"URL hostname '{hostname}' is blocked (internal/private)"

        try:
            ip_str = socket.gethostbyname(hostname)
            ip = ipaddress.ip_address(ip_str)
        except socket.gaierror:
            return False, f"Cannot resolve hostname: {hostname}"
        except ValueError as e:
            return False, f"Invalid IP address from hostname resolution: {e}"

        for network in BLOCKED_NETWORKS:
            if ip in network:
                return False, f"URL resolves to blocked IP range {network} (private/internal network)"

        if ip.is_loopback or ip.is_link_local or ip.is_private:
            return False, f"URL resolves to private/internal IP address: {ip}"

        return True, ""

    except Exception as e:
        return False, f"Invalid URL: {e}"
