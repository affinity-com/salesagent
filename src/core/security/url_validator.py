"""URL validation to prevent SSRF attacks.

Single source of truth for blocked networks and hostnames used by both
property list resolution and webhook URL validation.
"""

import ipaddress
import logging
import socket
from urllib.parse import ParseResult, quote, urlparse

logger = logging.getLogger(__name__)

# Placeholder logged in place of a URL with no usable scheme+host.
UNPARSEABLE_URL_FOR_LOG = "<unparseable-url>"

# Control characters are the entire mechanism behind log forging: a CR/LF in
# untrusted text lets it terminate the current record and append a fabricated one.
_CONTROL_CHAR_ESCAPES = {codepoint: f"\\x{codepoint:02x}" for codepoint in range(0x20)} | {0x7F: "\\x7f"}


def log_safe_text(value: object) -> str:
    """Escape control characters so untrusted text cannot forge a log record.

    Printable characters are left intact, so messages stay readable — only the
    CR/LF/NUL class that makes log injection possible is neutralized.
    """
    return str(value).translate(_CONTROL_CHAR_ESCAPES)


def url_for_log(url: str | None) -> str:
    """Render a URL for a log line: ``scheme://host/path``, percent-encoded.

    Never logs a raw URL, guarding two hazards at once:

    - **Log forging** — an admin- or buyer-supplied URL is unvalidated request
      data; percent-encoding removes every control character it could carry.
    - **Credential leakage** — userinfo and query string are dropped, so a token
      embedded in either never reaches the log.

    Returns :data:`UNPARSEABLE_URL_FOR_LOG` when there is no usable scheme+host.
    """
    if not url:
        return UNPARSEABLE_URL_FOR_LOG
    parsed = urlparse(str(url))
    if not (parsed.scheme and parsed.hostname):
        return UNPARSEABLE_URL_FOR_LOG
    return quote(f"{parsed.scheme}://{parsed.hostname}{parsed.path or ''}", safe=":/._-~")


# Blocked IP ranges (RFC 1918 private networks, loopback, link-local,
# CGNAT shared space, and multicast).
BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT (RFC 6598)
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),  # IPv6 multicast (AdCP L1 SSRF step 2)
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64 well-known prefix (RFC 6052)
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


def _scheme_error(parsed: ParseResult, *, require_https: bool) -> str | None:
    if require_https:
        if parsed.scheme != "https":
            return f"URL must use HTTPS scheme, got '{parsed.scheme}'"
        return None
    if parsed.scheme not in ("http", "https"):
        return "URL must use http or https protocol"
    return None


def _blocked_ip_error(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    for network in BLOCKED_NETWORKS:
        if ip in network:
            return f"URL resolves to blocked IP range {network} (private/internal network)"
    if ip.is_loopback or ip.is_link_local or ip.is_private:
        return f"URL resolves to private/internal IP address: {ip}"
    return None


def _check_hostname_resolution(hostname: str, *, resolve_dns: bool) -> tuple[bool, str]:
    """Literal-IP and optional DNS checks for a hostname already known to be non-blocked."""
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        error = _blocked_ip_error(literal_ip)
        return (False, error) if error else (True, "")

    if not resolve_dns:
        return True, ""

    try:
        ip = ipaddress.ip_address(socket.gethostbyname(hostname))
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"
    except ValueError:
        # The resolver's exception text is diagnostic only and callers surface this
        # message verbatim to the requester, so keep it server-side (CodeQL
        # ``py/stack-trace-exposure``).
        logger.warning("Hostname %s resolved to an unparseable address", log_safe_text(hostname), exc_info=True)
        return False, "Hostname resolved to an invalid IP address"

    error = _blocked_ip_error(ip)
    return (False, error) if error else (True, "")


def check_url_ssrf(
    url: str,
    *,
    require_https: bool = False,
    resolve_dns: bool = True,
) -> tuple[bool, str]:
    """Check a URL for SSRF safety.

    Validates that the URL does not target private/internal networks
    or cloud metadata services.

    Args:
        url: The URL to validate.
        require_https: If True, reject non-HTTPS schemes. If False,
            allow both HTTP and HTTPS.
        resolve_dns: If True (default), resolve the hostname and reject
            private/link-local results. If False, only apply scheme,
            blocked-hostname, and literal-IP checks — used at webhook
            *registration* so fixture hostnames (e.g. ``buyer.example.com``)
            are not rejected for NXDOMAIN; send-time still uses DNS.

    Returns:
        (is_safe, error_message) -- is_safe is True if the URL is safe,
        error_message describes the problem if not.
    """
    try:
        parsed = urlparse(url)
        scheme_err = _scheme_error(parsed, require_https=require_https)
        if scheme_err:
            return False, scheme_err

        hostname = parsed.hostname
        if not hostname:
            return False, "URL must have a valid hostname"

        if hostname.lower() in BLOCKED_HOSTNAMES:
            return False, f"URL hostname '{hostname}' is blocked (internal/private)"

        return _check_hostname_resolution(hostname, resolve_dns=resolve_dns)

    except Exception:
        # The parse/resolve failure detail is diagnostic only. Callers surface this
        # message verbatim to the requester (flash / JSON error), so keep the
        # exception text server-side rather than echoing it back — the raw text can
        # carry internal resolver state (CodeQL ``stack-trace-exposure``).
        logger.warning("SSRF check could not parse URL %s", url_for_log(url), exc_info=True)
        return False, "URL could not be parsed or resolved"
