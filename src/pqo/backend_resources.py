"""CPU delta and sampled resident memory of a verified local PostgreSQL backend."""
from dataclasses import dataclass
from ipaddress import ip_address
import sys
from threading import Event, Thread


@dataclass(frozen=True)
class BackendResources:
    cpu_ms: float
    initial_rss_bytes: int
    sampled_peak_rss_bytes: int
    samples: int


class BackendResourceMonitor:
    def __init__(self, connection):
        self.metrics = None
        self._process = None
        self._stop = Event()
        self._sampler = None
        self._samples = 0
        self._peak = 0
        self._failed = False
        try:
            import psutil
        except ImportError:
            return
        self._errors = (psutil.Error, OSError)
        server, port, client, client_port = connection.execute(
            'SELECT pg_catalog.inet_server_addr(), pg_catalog.inet_server_port(), '
            'pg_catalog.inet_client_addr(), pg_catalog.inet_client_port()'
        ).fetchone()
        if server is None or client is None or not all(ip_address(addr).is_loopback for addr in (server, client)):
            return
        # A server PID alone can accidentally select an unrelated local process.
        # Windows reports the accepted socket under the postmaster that created it.
        def owns_connection(process):
            return any(sock.status == psutil.CONN_ESTABLISHED and sock.raddr
                       and (ip_address(sock.laddr.ip), sock.laddr.port) == (ip_address(server), port)
                       and (ip_address(sock.raddr.ip), sock.raddr.port) == (ip_address(client), client_port)
                       for sock in process.net_connections(kind='tcp'))

        try:
            process = psutil.Process(connection.info.backend_pid)
            if owns_connection(process):
                self._process = process
            elif sys.platform == 'win32' and process.name().lower() == 'postgres.exe':
                parent = process.parent()
                if parent is not None and parent.exe() == process.exe() and owns_connection(parent):
                    self._process = process
        except self._errors:
            pass

    def _observe(self):
        self._peak = max(self._peak, self._process.memory_info().rss)
        self._samples += 1

    def _sample(self):
        while not self._stop.wait(0.01):
            try:
                self._observe()
            except self._errors:
                self._failed = True
                return

    def __enter__(self):
        if self._process is not None:
            try:
                self._observe()
                self._initial_rss = self._peak
                cpu = self._process.cpu_times()
                self._initial_cpu = cpu.user + cpu.system
            except self._errors:
                return self
            self._sampler = Thread(target=self._sample, daemon=True)
            self._sampler.start()
        return self

    def __exit__(self, exc_type, *_):
        if self._sampler is None:
            return
        try:
            cpu = self._process.cpu_times()
            elapsed = cpu.user + cpu.system - self._initial_cpu
        except self._errors:
            elapsed = -1
        finally:
            self._stop.set()
            self._sampler.join()
        if exc_type is None and not self._failed and elapsed >= 0:
            try:
                self._observe()
                if self._process.is_running():
                    self.metrics = BackendResources(elapsed * 1000, self._initial_rss, self._peak, self._samples)
            except self._errors:
                pass
