import subprocess
import time

from ai_sentinel import version


def test_service_version_matches_current_checkout():
    """Reflects however this exact checkout resolves: a VERSION file if one was written at deploy
    time (kolmogorov, rsync'd — no .git there), otherwise the live git SHA (local dev)."""
    version_file = version._ROOT / "VERSION"
    if version_file.exists():
        expected = version_file.read_text().strip()
    else:
        expected = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=version._ROOT
        ).decode().strip()
    assert version.SERVICE_VERSION == expected


def test_service_version_prefers_version_file(tmp_path, monkeypatch):
    version_file = tmp_path / "VERSION"
    version_file.write_text("deployed-abc123\n")
    monkeypatch.setattr(version, "_ROOT", tmp_path)
    assert version._detect_version() == "deployed-abc123"


def test_service_started_at_is_a_recent_timestamp():
    assert abs(time.time() - version.SERVICE_STARTED_AT) < 60
