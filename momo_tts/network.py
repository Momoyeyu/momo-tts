"""Direct IPv4 access to the console on one private network interface."""
from ipaddress import IPv4Address, IPv4Network
import socket

CONSOLE_PORT = 19890
PRIVATE_NETWORKS = tuple(IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def private_ipv4(value: str) -> bool:
    try:
        address = IPv4Address(value)
    except ValueError:
        return False
    return any(address in network for network in PRIVATE_NETWORKS)


def automatically_selected_address(ssh_connection: str) -> str:
    fields = ssh_connection.split()
    # sshd supplies the local interface actually reachable from the operator.
    if len(fields) == 4 and private_ipv4(fields[2]):
        return fields[2]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # UDP connect selects a route without sending a packet.
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
    except OSError:
        return ""
    return address if private_ipv4(address) else ""


def console_address(mode: str, ssh_connection: str = "") -> str:
    if mode == "loopback":
        return ""
    if mode == "auto":
        return automatically_selected_address(ssh_connection)
    if not private_ipv4(mode):
        raise ValueError("ADMIN_BIND must be auto, loopback or a private IPv4 address on this host")
    return mode
