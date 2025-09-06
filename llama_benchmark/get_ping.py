from urllib.parse import urlparse

from ping3 import ping  # ICMP ping


def get_ping_rtt(api_base):
    hostname = urlparse(api_base).hostname
    try:
        rtt = ping(hostname, timeout=1)  # returns RTT in seconds or None
        return rtt
    except Exception as e:
        print(f"Ping error: {e}")
        return None


import socket
import time


def tcp_ping(host, port=443, timeout=1):
    try:
        start = time.time()
        sock = socket.create_connection((host, port), timeout=timeout)
        end = time.time()
        sock.close()
        return end - start
    except Exception as e:
        print(f"TCP ping failed: {e}")
        return None
