from __future__ import annotations

import json
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    deploy_mode: str = "dry_run"
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"

    headnode_name: str = "server4"
    headnode_ssh_host: str = "10.0.10.4"
    headnode_ssh_port: int = 22
    headnode_ssh_user: str = "ubuntu"
    headnode_ssh_key_path: str = "/root/.ssh/id_ecdsa"

    workers_json: str = "[]"

    placement_service_url: str = "http://placement_service:9003"
    image_service_url: str = "http://image_service:9004"

    headnode_image_dir: str = "/var/lib/vms/images"
    worker_image_dir: str = "/var/lib/vms/images"
    image_sync_cache_dir: str = "/tmp/pucp-image-cache"

    @property
    def workers(self) -> list[dict]:
        return json.loads(self.workers_json)


settings = Settings()
