"""Namespace-scoped pod watcher.

The agent only ever calls *namespaced* APIs. It never lists pods across
the cluster — that is enforced by RBAC (a Role, not a ClusterRole), and
also by this code never calling list_pod_for_all_namespaces.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .detector import Failure, detect_failures

log = logging.getLogger("kubelantern.watcher")


class PodWatcher:
    def __init__(
        self,
        namespace: str,
        on_failure: Callable[[Failure], None],
        core_api=None,
        watch_timeout_seconds: int = 300,
        on_clear: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.namespace = namespace
        self.on_failure = on_failure
        # on_clear(namespace, pod, container): a container that was failing is
        # no longer failing (recovered, restarted into Running, or deleted).
        self.on_clear = on_clear or (lambda *_: None)
        if core_api is None:
            from kubernetes import client

            core_api = client.CoreV1Api()
        self.core = core_api
        self.watch_timeout_seconds = watch_timeout_seconds
        # Last restart count reported per observation key, so a pod stuck in
        # CrashLoopBackOff is observed again only when it restarts again.
        # Incident-level grouping and dedup live in agent.incident.
        self._reported: dict[tuple, int] = {}
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    # -- event handling ----------------------------------------------------

    def handle_pod(self, event_type: str, pod) -> list[Failure]:
        name = pod.metadata.name
        if event_type == "DELETED":
            self._forget_pod(name)
            return []

        emitted: list[Failure] = []
        current_keys = set()
        failing_containers = set()
        for f in detect_failures(pod):
            current_keys.add(f.key)
            failing_containers.add(f.container)
            if self._reported.get(f.key) == f.restarts:
                continue
            self._reported[f.key] = f.restarts
            emitted.append(f)
            self.on_failure(f)

        # Pod recovered (or switched reason) -> forget stale keys, and signal
        # "cleared" only for containers that are no longer failing at all.
        cleared = set()
        for key in [k for k in self._reported if k[1] == name and k not in current_keys]:
            del self._reported[key]
            if key[2] not in failing_containers:
                cleared.add(key[2])
        for container in cleared:
            self.on_clear(self.namespace, name, container)
        return emitted

    def _forget_pod(self, pod_name: str) -> None:
        containers = set()
        for key in [k for k in self._reported if k[1] == pod_name]:
            del self._reported[key]
            containers.add(key[2])
        for container in containers:
            self.on_clear(self.namespace, pod_name, container)

    # -- main loop ---------------------------------------------------------

    def _initial_sync(self) -> str:
        pods = self.core.list_namespaced_pod(self.namespace)
        present = {p.metadata.name for p in pods.items}
        # Pods deleted while we weren't watching never send DELETED.
        for gone in {k[1] for k in self._reported} - present:
            self._forget_pod(gone)
        for pod in pods.items:
            self.handle_pod("ADDED", pod)
        return pods.metadata.resource_version

    def process_event(self, event: dict, resource_version: str | None) -> str | None:
        """Handle one watch event and return the resource version to resume from.

        The kubernetes client yields ERROR events (and sometimes BOOKMARKs) as
        raw dicts rather than V1Pod objects, so those are handled explicitly.
        Raises WatchExpired when the API server says our resourceVersion is
        too old (HTTP 410) and a full relist is needed.
        """
        etype = event.get("type")
        obj = event.get("object")
        raw = event.get("raw_object") or (obj if isinstance(obj, dict) else {})

        if etype == "ERROR":
            code = raw.get("code")
            if code == 410:
                raise WatchExpired(raw.get("message", "resource version too old"))
            raise WatchError(f"watch ERROR event: code={code} message={raw.get('message')}")

        if etype == "BOOKMARK" or isinstance(obj, dict):
            rv = (raw.get("metadata") or {}).get("resourceVersion")
            return rv or resource_version

        self.handle_pod(etype, obj)
        return obj.metadata.resource_version or resource_version

    def run(self) -> None:
        from kubernetes import watch
        from kubernetes.client.exceptions import ApiException

        log.info("watching pods in namespace=%s", self.namespace)
        resource_version = self._initial_sync()
        backoff = 1

        while not self._stop:
            w = watch.Watch()
            try:
                for event in w.stream(
                    self.core.list_namespaced_pod,
                    namespace=self.namespace,
                    resource_version=resource_version,
                    timeout_seconds=self.watch_timeout_seconds,
                    allow_watch_bookmarks=True,
                ):
                    resource_version = self.process_event(event, resource_version)
                    if self._stop:
                        break
                backoff = 1
            except WatchExpired:
                log.info("watch expired (410), relisting")
                resource_version = self._safe_relist(resource_version)
            except ApiException as e:
                if e.status == 410:
                    log.info("watch expired (410), relisting")
                    resource_version = self._safe_relist(resource_version)
                    continue
                if e.status == 403:
                    log.error("forbidden: check the agent Role in namespace %s", self.namespace)
                log.warning("watch error %s, retrying in %ss", e.status, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except Exception:  # network blips, unexpected payloads
                log.exception("watch failed, relisting in %ss", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                # Never resume from a possibly-bad resourceVersion.
                resource_version = self._safe_relist(resource_version)
            finally:
                w.stop()

    def _safe_relist(self, fallback: str | None) -> str | None:
        try:
            return self._initial_sync()
        except Exception:
            log.exception("relist failed; will retry")
            return None  # None = watch from "now"; next loop resyncs on error


class WatchExpired(Exception):
    """The API server returned 410 Gone: resourceVersion too old."""


class WatchError(Exception):
    """The watch stream returned a non-410 ERROR event."""
