import re
import ssl
import threading
from contextlib import contextmanager

from pyVmomi import vim, vmodl
from pyVim.connect import SmartConnect, Disconnect
from pyVim.task import WaitForTask


def _ssl_context() -> ssl.SSLContext:
    return ssl._create_unverified_context()


class SessionManager:
    """Maintains one live ServiceInstance per vCenter/ESXi target.

    The first call for a given host connects and caches the session.
    Subsequent calls reuse it.  A lightweight ping (currentSession) detects
    an expired session before each use and transparently reconnects.
    """

    def __init__(self):
        self._sessions: dict[str, vim.ServiceInstance] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(target_cfg: dict) -> str:
        return f"{target_cfg['user']}@{target_cfg['host']}"

    @staticmethod
    def _connect(target_cfg: dict) -> vim.ServiceInstance:
        return SmartConnect(
            host=target_cfg["host"],
            user=target_cfg["user"],
            pwd=target_cfg["password"],
            sslContext=_ssl_context(),
        )

    @staticmethod
    def _ping(si: vim.ServiceInstance) -> bool:
        """Return True if the session is still valid."""
        try:
            si.content.sessionManager.currentSession
            return True
        except Exception:
            return False

    @staticmethod
    def _disconnect(si: vim.ServiceInstance) -> None:
        try:
            Disconnect(si)
        except Exception:
            pass

    def get(self, target_cfg: dict) -> vim.ServiceInstance:
        """Return a live ServiceInstance, reconnecting if necessary."""
        key = self._key(target_cfg)
        with self._lock:
            si = self._sessions.get(key)
            if si is None or not self._ping(si):
                if si is not None:
                    self._disconnect(si)
                si = self._connect(target_cfg)
                self._sessions[key] = si
            return si

    def invalidate(self, target_cfg: dict) -> None:
        """Remove the cached session so the next call reconnects."""
        key = self._key(target_cfg)
        with self._lock:
            si = self._sessions.pop(key, None)
            if si is not None:
                self._disconnect(si)


_session_manager = SessionManager()


@contextmanager
def vcenter_connection(target_cfg: dict):
    """Yield a shared ServiceInstance for *target_cfg*.

    The session is created on first use and reused across calls.  A lightweight
    ping checks liveness before yielding; a NotAuthenticated fault during the
    body invalidates the cache so the next call reconnects cleanly.
    """
    si = _session_manager.get(target_cfg)
    try:
        yield si
    except (vim.fault.NotAuthenticated, vmodl.fault.SecurityError):
        _session_manager.invalidate(target_cfg)
        raise


def get_obj(content, vimtypes: list, name: str):
    """Search the full inventory for a managed object by name. Returns None if not found."""
    container = content.viewManager.CreateContainerView(
        content.rootFolder, vimtypes, True
    )
    result = None
    for obj in container.view:
        if obj.name == name:
            result = obj
            break
    container.Destroy()
    return result


def _is_moref(value: str) -> bool:
    return bool(re.match(r"^vm-\d+$", value))


def lookup_vm(si, name_or_id: str):
    """
    Find a VM by moref ID (e.g. 'vm-42') or display name.
    Raises ValueError if not found or if multiple VMs share the same display name.
    """
    content = si.RetrieveContent()

    if _is_moref(name_or_id):
        ref = vim.VirtualMachine(name_or_id)
        ref._stub = si._stub
        return ref

    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    matches = [vm for vm in container.view if vm.name == name_or_id]
    container.Destroy()

    if not matches:
        raise ValueError(f"No VM found with name '{name_or_id}'")
    if len(matches) > 1:
        morefs = [vm._moId for vm in matches]
        raise ValueError(
            f"Multiple VMs named '{name_or_id}': {morefs}. Use a moref ID instead."
        )
    return matches[0]


def wait_for_task(task) -> None:
    """Wait for a vSphere task to complete. Raises on task error."""
    WaitForTask(task)
