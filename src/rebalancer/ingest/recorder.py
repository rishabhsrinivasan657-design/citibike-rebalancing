"""Kafka-free recorder for running on GitHub Actions (see docs/DESIGN.md).

Uses the same Collector as the streaming path, but buffers records in memory,
writes Parquet to a local folder, and uploads it to a Hugging Face dataset repo.

    recorder --minutes 165          # record, uploading every 15 min, then exit
    recorder --minutes 3 --no-upload  # local dry run
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Protocol

from rebalancer.config import Settings
from rebalancer.ingest.collector import Collector, StopFlag, http_client
from rebalancer.ingest.lake import LakeWriter

log = logging.getLogger("recorder")


class Uploader(Protocol):
    def upload(self, files: list[Path], root: Path) -> None: ...


class HuggingFaceUploader:
    """Uploads lake files to `<repo>/lake/...`, keeping the local partition layout."""

    def __init__(self, repo_id: str, token: str, api: Any = None) -> None:
        if api is None:
            from huggingface_hub import HfApi

            api = HfApi(token=token)
        self.api = api
        self.repo_id = repo_id
        self.api.create_repo(repo_id, repo_type="dataset", private=True, exist_ok=True)

    def upload(self, files: list[Path], root: Path) -> None:
        from huggingface_hub import CommitOperationAdd

        ops = [
            CommitOperationAdd(
                path_in_repo=f"lake/{f.relative_to(root).as_posix()}", path_or_fileobj=str(f)
            )
            for f in files
        ]
        self.api.create_commit(
            repo_id=self.repo_id,
            repo_type="dataset",
            operations=ops,
            commit_message=f"Add {len(files)} file(s)",
        )


class LakePublisher:
    """Publisher that buffers records per table and persists them on a timer.

    `Collector` calls `flush()` after every poll; here that means "write and upload
    if the persist interval has passed".
    """

    def __init__(
        self,
        settings: Settings,
        local_root: Path,
        uploader: Uploader | None,
        persist_every_s: float,
    ) -> None:
        self.topic_to_table = {
            settings.topic_station_status: "station_status",
            settings.topic_station_information: "station_information",
            settings.topic_poll_log: "poll_log",
        }
        self.root = local_root
        self.writer = LakeWriter(str(local_root))
        self.uploader = uploader
        self.persist_every_s = persist_every_s
        self.buffer: dict[str, list[dict[str, Any]]] = {t: [] for t in self.topic_to_table.values()}
        self._last_persist = time.monotonic()

    def publish(self, topic: str, key: str | None, value: dict[str, Any]) -> None:
        self.buffer[self.topic_to_table[topic]].append(value)

    def flush(self) -> None:
        if time.monotonic() - self._last_persist >= self.persist_every_s:
            self.persist()

    def persist(self) -> None:
        """Write buffered rows to Parquet, then try to upload every pending file."""
        for table, rows in self.buffer.items():
            if rows:
                self.writer.write(table, rows)
                log.info("wrote %d rows to %s", len(rows), table)
        self.buffer = {t: [] for t in self.buffer}
        self._last_persist = time.monotonic()
        self.upload_pending()

    def pending_files(self) -> list[Path]:
        return sorted(self.root.rglob("*.parquet"))

    def upload_pending(self) -> None:
        files = self.pending_files()
        if not files or self.uploader is None:
            return
        try:
            self.uploader.upload(files, self.root)
        except Exception as exc:  # keep files on disk and retry at the next persist
            log.error("upload of %d files failed, will retry: %s", len(files), exc)
            return
        for f in files:
            f.unlink()
        log.info("uploaded %d files", len(files))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, required=True, help="how long to record")
    parser.add_argument("--persist-minutes", type=float, default=15.0)
    parser.add_argument("--no-upload", action="store_true", help="write locally only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = Settings()
    root = Path(settings.lake_uri).resolve()
    root.mkdir(parents=True, exist_ok=True)

    uploader = None
    if not args.no_upload:
        repo, token = os.environ.get("HF_REPO"), os.environ.get("HF_TOKEN")
        if not repo or not token:
            sys.exit("HF_REPO and HF_TOKEN must be set (or pass --no-upload)")
        uploader = HuggingFaceUploader(repo, token)

    publisher = LakePublisher(settings, root, uploader, args.persist_minutes * 60)
    publisher.upload_pending()  # leftovers from an earlier failed run, if any

    stop = StopFlag(deadline=time.time() + args.minutes * 60)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with http_client(settings) as client:
        try:
            Collector(settings, client, publisher).run(stop)
        finally:
            publisher.persist()

    if uploader is not None and publisher.pending_files():
        sys.exit(f"{len(publisher.pending_files())} files were not uploaded; see {root}")


if __name__ == "__main__":
    main()
