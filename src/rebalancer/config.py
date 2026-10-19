"""Runtime configuration, read from environment variables (or a .env file)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gbfs_discovery_url: str = "https://gbfs.citibikenyc.com/gbfs/gbfs.json"
    gbfs_language: str = "en"
    http_timeout_s: float = 20.0
    http_retries: int = 3

    kafka_bootstrap: str = "localhost:19092"
    topic_station_status: str = "gbfs.station_status"
    topic_station_information: str = "gbfs.station_information"
    topic_poll_log: str = "gbfs.poll_log"

    poll_interval_s: int = 60
    info_interval_s: int = 3600
    info_max_age_s: int = 86400
    status_heartbeat_s: int = 900

    lake_uri: str = "./data/lake"
    sink_group_id: str = "lake-sink"
    sink_flush_interval_s: int = 300
    sink_max_buffer_rows: int = 200_000
