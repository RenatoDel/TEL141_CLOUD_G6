from __future__ import annotations

import os
import posixpath

import paramiko


class SSHClient:
    def __init__(self, host: str, user: str, port: int = 22, key_path: str | None = None):
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.client = None

    def connect(self):
        if self.client:
            return
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            key_filename=self.key_path,
            timeout=15,
        )

    def close(self):
        if self.client:
            self.client.close()
            self.client = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def execute(self, command: str, raise_on_error: bool = True):
        self.connect()
        stdin, stdout, stderr = self.client.exec_command(command)
        out = stdout.read().decode()
        err = stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        if raise_on_error and code != 0:
            raise RuntimeError(f"Comando falló en {self.host}: {command}\n{err.strip()}")
        return out, err

    def sudo(self, command: str, raise_on_error: bool = True):
        return self.execute(f"sudo {command}", raise_on_error=raise_on_error)

    def file_exists(self, path: str) -> bool:
        out, _ = self.sudo(f"test -f '{path}' && echo yes || echo no", raise_on_error=False)
        return out.strip() == "yes"

    def dir_exists(self, path: str) -> bool:
        out, _ = self.sudo(f"test -d '{path}' && echo yes || echo no", raise_on_error=False)
        return out.strip() == "yes"

    def upload_file(self, local_path: str, remote_path: str, mode: int = 0o644, owner: str = "root:root"):
        self.connect()
        tmp_remote = f"/tmp/.upload-{os.path.basename(remote_path)}-{os.getpid()}"

        sftp = self.client.open_sftp()
        try:
            sftp.put(local_path, tmp_remote)
            sftp.chmod(tmp_remote, mode)
        finally:
            sftp.close()

        remote_dir = posixpath.dirname(remote_path)
        self.sudo(f"mkdir -p '{remote_dir}'")
        self.sudo(f"mv '{tmp_remote}' '{remote_path}'")
        self.sudo(f"chmod {mode:o} '{remote_path}'")
        self.sudo(f"chown {owner} '{remote_path}'", raise_on_error=False)
