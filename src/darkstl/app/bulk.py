"""The queue: bulk actions on many models (collect, export as GLB, write packages, enhance) run one after
another, each as a job of the app's job system, listed on the Queue page with its progress and a Stop.

A mixin of :class:`darkstl.app.service.Service`. Each item does one action for a list of models, a model at a
time, through the same commands the page uses for one model (so a bulk export writes what Export would, and a
bulk enhance saves a variation as the Displace tab would). One model that fails doesn't stop the others: it's
listed with why. Stopping a waiting item takes it off the queue; stopping the running one ends it at its next
step, keeping what it has done.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

KEEP_FINISHED = 30  # finished items still listed

ACTIONS = {
    "collect": "Collect into the app",
    "packages": "Export packages",
    "export_glb": "Export as GLB",
    "enhance": "Enhance with the current settings",
}


@dataclass
class QueueItem:
    id: str
    action: str
    label: str
    ids: list[str]
    run: Callable[["Any"], Any] = field(repr=False)
    state: str = "waiting"  # waiting | running | done | failed | stopped
    job: str | None = None
    added: float = field(default_factory=time.time)
    result: Any = None
    error: str | None = None
    progress: float = 0.0
    message: str = ""

    def as_json(self, jobs) -> dict:
        job = jobs.get(self.job) if self.job else None
        progress, message = self.progress, self.message
        if job is not None and self.state == "running":
            progress, message = job.progress, job.message
        return {"id": self.id, "action": self.action, "label": self.label, "count": len(self.ids), "state": self.state,
                "job": self.job, "progress": round(progress, 4), "message": message, "result": self.result,
                "error": self.error, "added": self.added, "stopping": bool(job is not None and job.stopping)}


class QueueCommands:
    """The queue's commands (see the module doc)."""

    def _queue_init(self) -> None:
        self.queue: list[QueueItem] = []
        self._queue_lock = threading.Lock()
        self._queue_wake = threading.Condition(self._queue_lock)
        self._queue_thread: threading.Thread | None = None

    def _queue_add(self, action: str, label: str, ids: list[str], run: Callable) -> QueueItem:
        item = QueueItem(uuid.uuid4().hex[:10], action, label, list(ids), run)
        with self._queue_lock:
            self.queue.append(item)
            finished = [i for i in self.queue if i.state not in ("waiting", "running")]
            for old in finished[:-KEEP_FINISHED]:
                self.queue.remove(old)
            if self._queue_thread is None or not self._queue_thread.is_alive():
                self._queue_thread = threading.Thread(target=self._queue_loop, name="queue", daemon=True)
                self._queue_thread.start()
            self._queue_wake.notify_all()
        return item

    def _queue_loop(self) -> None:
        while True:
            with self._queue_lock:
                item = next((i for i in self.queue if i.state == "waiting"), None)
                if item is None:
                    self._queue_thread = None
                    return
                item.state = "running"
                job = self.jobs.start(item.label, item.run, stoppable=True)
                item.job = job.id
            job.finished.wait()
            with self._queue_lock:
                item.progress, item.message = job.progress, job.message
                item.result, item.error = job.result, job.error
                item.state = {"done": "done", "stopped": "stopped"}.get(job.state, "failed")

    def cmd_queue(self, args: dict) -> dict:
        """What's queued, running and finished lately, oldest first."""
        with self._queue_lock:
            items = [i.as_json(self.jobs) for i in self.queue]
        return {"items": items, "active": sum(1 for i in items if i["state"] in ("waiting", "running"))}

    def cmd_queue_stop(self, args: dict) -> dict:
        """Stop a queued item: one waiting is taken off the queue, the one running ends at its next step."""
        from .service import AppError

        with self._queue_lock:
            item = next((i for i in self.queue if i.id == str(args.get("id") or "")), None)
            if item is None:
                raise AppError("That isn't in the queue any more.")
            if item.state == "waiting":
                item.state, item.error = "stopped", "Taken off the queue"
            elif item.state == "running" and item.job:
                self.jobs.stop(item.job)
        return self.cmd_queue({})

    def cmd_queue_clear(self, args: dict) -> dict:
        """Take the finished items off the list."""
        with self._queue_lock:
            self.queue = [i for i in self.queue if i.state in ("waiting", "running")]
        return self.cmd_queue({})

    def cmd_queue_add(self, args: dict) -> dict:
        """Queue ``action`` (see ACTIONS) for the library models ``ids``. Collecting and writing packages go a game
        at a time (one item per game); exports and enhancing are one item."""
        from .service import AppError

        action = str(args.get("action") or "")
        if action not in ACTIONS:
            raise AppError(f"There's no bulk action '{action}'.")
        entries = [e for e in (self._entry(str(i)) for i in args.get("ids") or []) if e is not None]
        if not entries:
            raise AppError("Choose models in the library first.")
        added = []
        if action in ("collect", "packages"):
            by_game: dict[str, list] = {}
            for e in entries:
                by_game.setdefault(e.game, []).append(e)
            for game, these in by_game.items():
                n = len(these)
                what = "Collecting" if action == "collect" else "Writing packages of"
                label = f"{what} {n:,} model{'s' if n != 1 else ''} from {game}"
                added.append(self._queue_add(action, label, [e.id for e in these], self._bulk_collect(these, action == "packages")))
        else:
            n = len(entries)
            label = (f"Exporting {n:,} model{'s' if n != 1 else ''} as GLB" if action == "export_glb"
                     else f"Enhancing {n:,} model{'s' if n != 1 else ''}")
            run = self._bulk_export(entries, args) if action == "export_glb" else self._bulk_enhance(entries)
            added.append(self._queue_add(action, label, [e.id for e in entries], run))
        return {**self.cmd_queue({}), "added": [i.id for i in added]}

    # ------------------------------------------------------------ the actions

    def _await(self, parent, started: dict, span: tuple[float, float], what: str) -> Any:
        """Wait for a job started by a command (``started``: its {"job": id}) inside the queue's ``parent`` job,
        showing its progress in ``span`` of the parent's; a Stop of the parent stops it too."""
        from .service import AppError, Stopped

        job = self.jobs.get(started["job"])
        while not job.finished.wait(0.1):
            if parent.stopping:
                self.jobs.stop(job.id)
            else:
                parent.progress = span[0] + (span[1] - span[0]) * job.progress
                parent.message = f"{what}: {job.message}" if job.message else what
        if job.state == "stopped" or parent.stopping:
            raise Stopped()
        if job.state != "done":
            raise AppError(job.error or "It didn't work.")
        return job.result

    def _each(self, job, entries: list, what: str, one: Callable) -> dict:
        """Run ``one(entry, span)`` for each model in turn, reporting progress; a model that fails is listed
        with why, and the others go on."""
        from .service import AppError, Stopped

        done, failed, out = 0, [], []
        n = len(entries)
        for i, e in enumerate(entries):
            job.report(i / n, f"{what} {e.label} ({i + 1:,} of {n:,})")
            try:
                got = one(e, (i / n, (i + 1) / n))
            except Stopped:
                raise
            except (AppError, Exception) as exc:  # noqa: BLE001 - one model doesn't stop the rest
                failed.append({"id": e.id, "label": e.label, "error": str(exc) or type(exc).__name__})
                continue
            done += 1
            if got is not None:
                out.append(got)
        return {"done": done, "failed": failed, "items": out, "count": n}

    def _bulk_collect(self, entries: list, packages: bool) -> Callable:
        ids = [e.id for e in entries]

        def run(job):
            job.report(0.0, "Starting")
            started = self.cmd_collect_run({"ids": ids, "packages": packages, "animations": True, "textures": True})
            result = self._await(job, started, (0.0, 1.0), "Writing packages" if packages else "Copying")
            return {"done": len(ids), "failed": [{"label": f, "error": ""} for f in (result.get("failed") or [])]
                    if packages else [], "count": len(ids), "folder": result.get("folder"), "bytes": result.get("bytes")}

        return run

    def _bulk_export(self, entries: list, args: dict) -> Callable:
        folder = str(args.get("folder") or self.config.export_folder)

        def run(job):
            def one(e, span):
                with self._opened(e) as sid:
                    started = self.cmd_export({"sid": sid, "which": "original", "format": ".glb", "units": "mm",
                                               "textures": True, "folder": folder})
                    return self._await(job, started, span, f"Exporting {e.label}")["path"]

            out = self._each(job, entries, "Exporting", one)
            return {**out, "folder": folder}

        return run

    def _bulk_enhance(self, entries: list) -> Callable:
        def run(job):
            def one(e, span):
                if e.config is not None:
                    from .service import AppError

                    raise AppError("Configurable models are enhanced one at a time, from their page (their "
                                   "variations can't be saved yet).")
                settings = self._last_settings(e.game)
                mid = span[0] + 0.85 * (span[1] - span[0])
                with self._opened(e) as sid:
                    self._await(job, self.cmd_enhance({"sid": sid, "settings": settings, "bulk": True}), (span[0], mid),
                                f"Enhancing {e.label}")
                    saved = self._await(job, self.cmd_variation_save({"sid": sid, "name": ""}), (mid, span[1]),
                                        f"Saving {e.label}")
                return saved["variation"]["name"]

            return self._each(job, entries, "Enhancing", one)

        return run

    @contextmanager
    def _opened(self, entry):
        """A model opened for a bulk action, closed again after (unless it was open already, on its page), so a
        long queue doesn't close the models the user has open."""
        with self.lock:
            before = set(self.sessions)
        sid = self.cmd_model_open({"id": entry.id})["sid"]
        try:
            yield sid
        finally:
            if sid not in before:
                with self.lock:
                    self.sessions.pop(sid, None)

    def _last_settings(self, game: str) -> dict:
        """The settings the Mesh and Displace tabs last generated a model of ``game`` with (else the defaults)."""
        return dict((self.config.last_enhance or {}).get(game) or {})


__all__ = ["ACTIONS", "QueueCommands", "QueueItem"]
