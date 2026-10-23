"""Cooperative cancellation and PostgreSQL cancellation for deep analysis."""

from contextlib import contextmanager
from threading import Event, Thread


class AnalysisCancelled(RuntimeError):
    pass


class AnalysisControl:
    def __init__(self, progress=None):
        self.cancelled = Event()
        self.progress = progress or (lambda message: None)

    def cancel(self):
        self.cancelled.set()

    def check(self):
        if self.cancelled.is_set():
            raise AnalysisCancelled("Анализ отменён пользователем; пробные изменения откачены.")

    def report(self, message):
        self.check()
        self.progress(message)

    @contextmanager
    def monitor(self, connection):
        """Interrupt running SQL without blocking the GUI thread."""
        stopped = Event()

        def watch():
            while not stopped.wait(0.1):
                if self.cancelled.is_set():
                    try:
                        connection.cancel_safe(timeout=2)
                    except Exception:
                        # Cooperative checkpoints still stop work between statements.
                        pass
                    return

        self.check()
        watcher = Thread(target=watch, daemon=True)
        watcher.start()
        try:
            yield
            self.check()
        except Exception:
            self.check()
            raise
        finally:
            stopped.set()
            watcher.join(timeout=3)
