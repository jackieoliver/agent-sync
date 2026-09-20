"""Pick a reachable address for the Mac: Tailscale first, then direct Ethernet.

Both addresses are pinned in mac-known-hosts, so host identity is still verified.
If neither answers, return the Tailscale address so errors read as network errors.
"""
import socket

from syncconf import CONF

USER = CONF['mac_user']
CANDIDATES = tuple(CONF['mac_hosts'])


def address(timeout=3.0):
    for host in CANDIDATES:
        try:
            with socket.create_connection((host, 22), timeout=timeout):
                return host
        except OSError:
            continue
    return CANDIDATES[0]


def target(timeout=3.0):
    return USER + '@' + address(timeout)
