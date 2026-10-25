"""Windows UDP / event-loop compatibility.

* Windows reports an ICMP "port unreachable" for a UDP packet we sent as a
  WSAECONNRESET error on our *next* receive. Disable that per socket
  (SIO_UDP_CONNRESET), as most UDP programs on Windows do.
* Python's default Windows event loop (Proactor) handles UDP sockets less
  well than the select-based loop (errors after resets, transports that need
  extra loop turns to close). The session uses the selector loop everywhere.
"""

import asyncio
import socket
import sys


def udp_socket(local_port, remote_addr):
    """A connected IPv4 UDP socket, ready for create_datagram_endpoint(sock=...)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if sys.platform == "win32":
            try:
                s.ioctl(socket.SIO_UDP_CONNRESET, False)
            except (AttributeError, OSError):
                pass
        s.bind(("0.0.0.0", local_port))
        if remote_addr is not None:
            s.connect(remote_addr)
        s.setblocking(False)
        return s
    except OSError:
        s.close()
        raise


def run(coro):
    """asyncio.run() on a selector event loop on every platform."""
    loop = asyncio.SelectorEventLoop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(asyncio.sleep(0))      # let transports finish closing
        finally:
            asyncio.set_event_loop(None)
            loop.close()
