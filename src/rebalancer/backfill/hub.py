"""Keeps the raw history in a Hugging Face dataset, so the backfill can run on
throwaway GitHub machines: pull what's already there, push each piece as it's done.

Layout in the dataset mirrors the local folder: `raw/trips/month=YYYY-MM/...`,
`raw/weather/...`, `raw/holidays/...`, `raw/_checks.md`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rebalancer.ingest.recorder import resolve_repo_id

log = logging.getLogger(__name__)


class HubSync:
    def __init__(self, repo: str, token: str, raw: Path, api: Any = None) -> None:
        if raw.name != "raw":
            raise ValueError("with --hf, --raw must point to a folder named 'raw'")
        if api is None:
            from huggingface_hub import HfApi

            api = HfApi(token=token)
        self.api = api
        self.token = token
        self.raw = raw
        self.repo_id = resolve_repo_id(api, repo, need_write=True)
        api.create_repo(self.repo_id, repo_type="dataset", private=True, exist_ok=True)
        log.info("syncing history with Hugging Face dataset %s", self.repo_id)

    def pull(self) -> None:
        """Download everything already in the dataset, so finished months are skipped."""
        from huggingface_hub import snapshot_download

        snapshot_download(
            self.repo_id,
            repo_type="dataset",
            token=self.token,
            local_dir=self.raw.parent,
            allow_patterns=["raw/**"],
        )

    def push(self, path: Path) -> None:
        rel = path.relative_to(self.raw).as_posix()
        target = f"raw/{rel}"
        if path.is_dir():
            self.api.upload_folder(
                repo_id=self.repo_id,
                repo_type="dataset",
                folder_path=str(path),
                path_in_repo=target,
                commit_message=f"Update {target}",
            )
        else:
            self.api.upload_file(
                repo_id=self.repo_id,
                repo_type="dataset",
                path_or_fileobj=str(path),
                path_in_repo=target,
                commit_message=f"Update {target}",
            )
        log.info("uploaded %s", target)
