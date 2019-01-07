import logging
import socket

from io import BytesIO

try:
    from BaseHTTPServer import BaseHTTPRequestHandler
except ImportError:
    from http.server import BaseHTTPRequestHandler

import eventlet


log = logging.getLogger(__name__)


class HTTPRequest(BaseHTTPRequestHandler):
    def __init__(self, request_text):
        self.rfile = BytesIO(request_text)
        self.raw_requestline = self.rfile.readline()
        self.error_code = self.error_message = None
        self.parse_request()

    def send_error(self, code, message):
        self.error_code = code
        self.error_message = message


class HTTPServer(object):
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.host = self.port = None
        self.bound = False
        self.clients = set()
        self.accepter = None

    @property
    def addresses(self):
        if self.host:
            return [self.host]

        addrs = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), self.port,
                                           socket.AF_INET):
                addrs.add(info[4][0])
        except socket.gaierror:
            pass

        addrs.add("127.0.0.1")
        return sorted(addrs)

    @property
    def urls(self):
        for addr in self.addresses:
            yield "http://{0}:{1}/".format(addr, self.port)

    @property
    def url(self):
        return next(self.urls, None)

    def bind(self, host="127.0.0.1", port=0):
        try:
            self.socket.bind((host or "", port))
        except socket.error as err:
            raise OSError(err)

        self.socket.listen(1)
        self.bound = True
        self.host, self.port = self.socket.getsockname()
        if self.host == "0.0.0.0":
            self.host = None

    def accept_client(self):
        try:
            conn, _ = self.socket.accept()
            conn.settimeout(None)
        except socket.timeout:
            raise OSError("Socket accept timed out")

        try:
            req_data = conn.recv(1024)
        except socket.error:
            raise OSError("Failed to read data from socket")

        req = HTTPRequest(req_data)
        if req.command not in ("GET", "HEAD"):
            conn.send(b"HTTP/1.1 501 Not Implemented\r\n")
            conn.close()
            raise OSError("Invalid request method: {0}".format(req.command))

        try:
            conn.send(b"HTTP/1.1 200 OK\r\n")
            conn.send(b"Server: Streamlink\r\n")
            conn.send(b"Content-Type: video/mp4\r\n")
            conn.send(b"\r\n")
        except socket.error:
            raise OSError("Failed to write data to socket")

        # We don't want to send any data on HEAD requests.
        if req.command == "HEAD":
            conn.close()
            raise OSError

        self.clients.add(conn)

        user_agent = req.headers.get("User-Agent") or "unknown player"
        log.info("Got HTTP request from {0}".format(user_agent))

    def accept_loop(self):
        while True:
            try:
                self.accept_client()
            except Exception:
                log.exception('Exception in accept_loop, continuing')

    def open(self, timeout=30):
        self.socket.settimeout(timeout)
        self.accept_client()
        self.accepter = eventlet.spawn_n(self.accept_loop)

    def write(self, data):
        if not self.clients:
            raise IOError("No connection")

        for conn in list(self.clients):
            try:
                conn.sendall(data)
            except Exception:
                log.exception('Exception sending data to conn, removing')
                try:
                    conn.close()
                except Exception:
                    pass
                self.clients.remove(conn)

    def close(self, client_only=False):
        if self.accepter:
            self.accepter.throw()

        for conn in self.clients:
            conn.close()

        if not client_only:
            try:
                self.socket.shutdown(2)
            except (OSError, socket.error):
                pass
            self.socket.close()
