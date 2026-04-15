"""URL validation to prevent SSRF attacks.

Single source of truth for blocked networks and hostnames used by both
property list resolution and webhook URL validation.
"""

import ipaddress
import socket
from urllib.parse import urlparse

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


def _is_localhost_subdomain(hostname: str) -> bool:
    """Return True if hostname ends with .localhost (RFC 6761 special-use TLD).

    RFC 6761 reserves the .localhost TLD as a special-use domain that always
    refers to the local machine. Services named <name>.localhost are explicitly
    local by convention — unlike bare Docker service names (e.g. si-agent) which
    are opaque and resolve to private RFC-1918 addresses inside container networks.

    Allowing *.localhost bypasses IP-range checks without a blanket flag, so only
    explicitly-named local services can be registered as TMP provider endpoints.
    The bare hostname "localhost" itself remains in BLOCKED_HOSTNAMES and is
    rejected before this check is reached.

    Examples:
        si-agent.localhost       → True  (allowed: explicitly local)
        tmp-router.localhost     → True  (allowed: explicitly local)
        host.docker.internal     → False (blocked by BLOCKED_HOSTNAMES first)
        localhost                → False (blocked by BLOCKED_HOSTNAMES first)
        example.com              → False (public domain, goes through IP checks)
    """
    return hostname.lower().endswith(".localhost")


def check_url_ssrf(
    url: str,
    *,
    require_https: bool = False,
) -> tuple[bool, str]:
    """Check a URL for SSRF safety.

    Validates that the URL does not target private/internal networks
    or cloud metadata services.

    *.localhost hostnames (RFC 6761) are permitted without IP-range checks —
    they are explicitly local by naming convention (e.g. http://si-agent.localhost:3003).
    Bare Docker service names (e.g. http://si-agent:3003) and Docker-internal
    hostnames (e.g. http://host.docker.internal) remain blocked.

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

        # Always block known-dangerous hostnames regardless of other checks
        if hostname.lower() in BLOCKED_HOSTNAMES:
            return False, f"URL hostname '{hostname}' is blocked (internal/private)"

        # *.localhost hostnames are RFC 6761 special-use — explicitly local by
        # naming convention. Skip IP-range checks for these only.
        if _is_localhost_subdomain(hostname):
            return True, ""

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
