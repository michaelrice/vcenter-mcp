import pytest
from unittest.mock import MagicMock, patch, call
from vcenter_mcp.client import _is_moref, lookup_vm, SessionManager, vcenter_connection


def test_is_moref_valid():
    assert _is_moref("vm-42") is True
    assert _is_moref("vm-1") is True
    assert _is_moref("vm-99999") is True


def test_is_moref_invalid():
    assert _is_moref("my-vm") is False
    assert _is_moref("vm-") is False
    assert _is_moref("42") is False
    assert _is_moref("VM-42") is False
    assert _is_moref("vm-42x") is False


def test_lookup_vm_not_found_raises():
    si = MagicMock()
    content = si.RetrieveContent.return_value
    view = content.viewManager.CreateContainerView.return_value
    view.view = []

    with pytest.raises(ValueError, match="No VM found"):
        lookup_vm(si, "ghost-vm")


def test_lookup_vm_duplicate_name_raises():
    si = MagicMock()
    content = si.RetrieveContent.return_value
    view = content.viewManager.CreateContainerView.return_value

    vm1 = MagicMock()
    vm1.name = "test-vm"
    vm1._moId = "vm-1"
    vm2 = MagicMock()
    vm2.name = "test-vm"
    vm2._moId = "vm-2"
    view.view = [vm1, vm2]

    with pytest.raises(ValueError, match="Multiple VMs"):
        lookup_vm(si, "test-vm")


def test_lookup_vm_by_name_returns_match():
    si = MagicMock()
    content = si.RetrieveContent.return_value
    view = content.viewManager.CreateContainerView.return_value

    vm = MagicMock()
    vm.name = "test-vm"
    view.view = [vm]

    result = lookup_vm(si, "test-vm")
    assert result is vm


def test_lookup_vm_by_moref_returns_reference():
    from pyVmomi import vim
    si = MagicMock()
    si._stub = MagicMock()

    result = lookup_vm(si, "vm-42")

    assert isinstance(result, vim.VirtualMachine)
    assert result._stub is si._stub
    # Verify container view was NOT created (moref path skips name search)
    content = si.RetrieveContent.return_value
    assert not content.viewManager.CreateContainerView.called


# ---------------------------------------------------------------------------
# SessionManager tests
# ---------------------------------------------------------------------------

_CFG = {"host": "vc.example.com", "user": "admin", "password": "secret"}


def _make_manager():
    """Return a fresh SessionManager with SmartConnect patched out."""
    return SessionManager()


def test_session_manager_connects_on_first_call():
    mgr = _make_manager()
    fake_si = MagicMock()
    with patch("vcenter_mcp.client.SmartConnect", return_value=fake_si) as mock_connect, \
         patch.object(SessionManager, "_ping", return_value=True):
        result = mgr.get(_CFG)
    mock_connect.assert_called_once()
    assert result is fake_si


def test_session_manager_reuses_cached_session():
    mgr = _make_manager()
    fake_si = MagicMock()
    with patch("vcenter_mcp.client.SmartConnect", return_value=fake_si) as mock_connect, \
         patch.object(SessionManager, "_ping", return_value=True):
        first = mgr.get(_CFG)
        second = mgr.get(_CFG)
    assert mock_connect.call_count == 1
    assert first is second


def test_session_manager_reconnects_when_ping_fails():
    mgr = _make_manager()
    si_a, si_b = MagicMock(), MagicMock()
    with patch("vcenter_mcp.client.SmartConnect", side_effect=[si_a, si_b]) as mock_connect, \
         patch.object(SessionManager, "_ping", return_value=False), \
         patch.object(SessionManager, "_disconnect") as mock_disconnect:
        first = mgr.get(_CFG)
        second = mgr.get(_CFG)
    assert mock_connect.call_count == 2
    assert first is si_a
    assert second is si_b
    mock_disconnect.assert_called_once_with(si_a)


def test_session_manager_invalidate_forces_reconnect():
    mgr = _make_manager()
    si_a, si_b = MagicMock(), MagicMock()
    with patch("vcenter_mcp.client.SmartConnect", side_effect=[si_a, si_b]), \
         patch.object(SessionManager, "_ping", return_value=True), \
         patch.object(SessionManager, "_disconnect") as mock_disconnect:
        mgr.get(_CFG)
        mgr.invalidate(_CFG)
        result = mgr.get(_CFG)
    assert result is si_b
    mock_disconnect.assert_called_once_with(si_a)


def test_session_manager_key_isolates_targets():
    mgr = _make_manager()
    cfg_a = {"host": "vc-a", "user": "u", "password": "p"}
    cfg_b = {"host": "vc-b", "user": "u", "password": "p"}
    si_a, si_b = MagicMock(), MagicMock()
    with patch("vcenter_mcp.client.SmartConnect", side_effect=[si_a, si_b]), \
         patch.object(SessionManager, "_ping", return_value=True):
        result_a = mgr.get(cfg_a)
        result_b = mgr.get(cfg_b)
    assert result_a is si_a
    assert result_b is si_b


def test_vcenter_connection_invalidates_on_not_authenticated():
    from pyVmomi import vim
    import vcenter_mcp.client as client_mod
    fake_si = MagicMock()
    with patch.object(client_mod._session_manager, "get", return_value=fake_si), \
         patch.object(client_mod._session_manager, "invalidate") as mock_invalidate:
        with pytest.raises(vim.fault.NotAuthenticated):
            with vcenter_connection(_CFG):
                raise vim.fault.NotAuthenticated()
    mock_invalidate.assert_called_once_with(_CFG)
