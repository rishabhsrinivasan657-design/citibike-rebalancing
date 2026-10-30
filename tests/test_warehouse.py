from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from rebalancer.warehouse import stale_lfs_files

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


def lfs(oid, hours_ago):
    return SimpleNamespace(file_oid=oid, pushed_at=NOW - timedelta(hours=hours_ago), size=1)


def test_prune_drops_only_old_unused_copies():
    files = [lfs("current", 30), lfs("old", 30), lfs("uploading", 0.5)]
    stale = stale_lfs_files(files, keep={"current"}, now=NOW, min_age=timedelta(hours=2))
    assert [f.file_oid for f in stale] == ["old"]
