"""Task 15 Task 11 corrective v2, Section 10 ("Authority metadata"):
functional success and release authority are distinct. A macOS
development run may PASS functionally but must never be indistinguishable
from an authoritative Linux release run.
"""

from scripts.release_gate.environment import Authority, compute_authority


def test_compute_authority_on_linux_is_authoritative():
    authority = compute_authority(platform_name="Linux")
    assert authority.authoritative is True
    assert authority.platform == "linux"
    assert authority.reason is None


def test_compute_authority_on_darwin_is_not_authoritative():
    authority = compute_authority(platform_name="Darwin")
    assert authority.authoritative is False
    assert authority.platform == "darwin"
    assert authority.reason == "development_only_platform"


def test_compute_authority_on_windows_is_not_authoritative():
    authority = compute_authority(platform_name="Windows")
    assert authority.authoritative is False
    assert authority.reason == "development_only_platform"


def test_authority_is_a_frozen_value_object():
    authority = Authority(authoritative=True, platform="linux", reason=None)
    try:
        authority.authoritative = False
    except AttributeError:
        pass
    else:
        raise AssertionError("Authority must be frozen")
