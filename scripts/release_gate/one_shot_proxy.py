"""Task 15 Tasks 7-10 corrective, Important 2 (N3): a test-only,
real TCP one-shot response-dropping proxy.

Sits between a real Node HttpSyncTransport and the real Central HTTP
endpoint so N3 can prove a genuine ACK-loss scenario: Central fully
receives and durably commits the request (Django's `transaction.atomic()`
in `linuxlab.sync.ingestion.ingest_event()` commits before the view
returns, and therefore before Gunicorn ever starts writing response
bytes back), but the response is deliberately never relayed to Node --
exactly what a lost ACK looks like from the Node's own observable
state. No response is fabricated, no trusted row is created or mutated
directly, and Central's own real duplicate-handling is exercised by a
real second delivery attempt afterward.

Only the FIRST proxied connection is intercepted this way. Every
connection after that is forwarded unmodified in both directions, so a
normal retry through the SAME proxy (if a caller chooses to keep using
it) behaves exactly like a direct connection to Central.
"""

import socket
import threading


def _read_http_message(sock):
    """Reads one complete HTTP/1.1 message (request or response) off
    `sock`: headers up to the blank line, then exactly Content-Length
    body bytes. Returns the raw bytes of the full message, or None if
    the peer closed before a complete message ever arrived. This
    module's traffic is always a real Django/Gunicorn JSON exchange
    with an explicit Content-Length -- never chunked -- so this is
    sufficient without a general-purpose HTTP parser."""
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            return buffer or None
        buffer += chunk
    header_end = buffer.index(b"\r\n\r\n") + 4
    header_bytes = buffer[:header_end]
    body = buffer[header_end:]

    content_length = 0
    for line in header_bytes.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
            break

    while len(body) < content_length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk

    return header_bytes + body


class OneShotResponseDroppingProxy:
    """Binds a caller-supplied `ReservedListener` (exactly like
    `start_gunicorn()` itself uses -- no window between reservation and
    use). The FIRST accepted downstream connection: its request is
    forwarded to the real upstream (Central), the upstream's full
    response is read to completion (proving Central already committed),
    and then the downstream connection is closed with NOTHING written
    back -- a genuine dropped ACK, not a fabricated one. Every
    connection after that is forwarded unmodified in both directions.
    """

    def __init__(self, listener, upstream_host, upstream_port):
        self._listener = listener
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._first_connection_claimed = False
        self._claim_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._stop_requested = False
        self._accept_thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self._listener.host}:{self._listener.port}"

    def start(self) -> None:
        self._listener.sock.settimeout(0.5)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while not self._stop_requested:
            try:
                downstream, _addr = self._listener.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._claim_lock:
                drop_response = not self._first_connection_claimed
                self._first_connection_claimed = True
            thread = threading.Thread(
                target=self._handle_connection, args=(downstream, drop_response), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _handle_connection(self, downstream: socket.socket, drop_response: bool) -> None:
        try:
            request_bytes = _read_http_message(downstream)
            if not request_bytes:
                return
            with socket.create_connection(
                (self._upstream_host, self._upstream_port), timeout=15
            ) as upstream:
                upstream.sendall(request_bytes)
                response_bytes = _read_http_message(upstream)
            if drop_response:
                # Upstream (Central) has already fully responded --
                # therefore already committed -- the response is simply
                # never relayed downstream.
                return
            if response_bytes:
                downstream.sendall(response_bytes)
        except OSError:
            pass
        finally:
            downstream.close()

    def stop(self) -> None:
        self._stop_requested = True
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)
        for thread in self._threads:
            thread.join(timeout=5)
        self._listener.sock.close()
