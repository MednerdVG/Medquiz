"""Private object storage for uploads and rendered PDFs.

Private by default; files are only ever served through short-lived signed URLs.
Backends: local filesystem (dev / single VM) or S3-compatible (set STORAGE_BACKEND=s3).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import quote

from app.config import settings


class Storage:
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def signed_url(self, key: str, ttl_seconds: int = 300, filename: str | None = None) -> str: ...


def _sign(key: str, exp: int) -> str:
    return hmac.new(settings().secret_key.encode(), f"{key}|{exp}".encode(), hashlib.sha256).hexdigest()


def verify_signature(key: str, exp: int, sig: str) -> bool:
    return exp >= int(time.time()) and hmac.compare_digest(_sign(key, exp), sig)


class LocalStorage(Storage):
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid storage key")
        return p

    def put(self, key, data, content_type):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key):
        return self._path(key).read_bytes()

    def delete(self, key):
        p = self._path(key)
        if p.exists():
            p.unlink()

    def signed_url(self, key, ttl_seconds=300, filename=None):
        exp = int(time.time()) + ttl_seconds
        q = f"?exp={exp}&sig={_sign(key, exp)}" + (f"&name={quote(filename)}" if filename else "")
        return f"{settings().base_url}/files/{quote(key)}{q}"


class S3Storage(Storage):  # pragma: no cover - needs a bucket
    def __init__(self):
        import boto3
        s = settings()
        self.bucket = s.s3_bucket
        self.client = boto3.client("s3", region_name=s.s3_region, endpoint_url=s.s3_endpoint or None)

    def put(self, key, data, content_type):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                               ServerSideEncryption="AES256")

    def get(self, key):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def signed_url(self, key, ttl_seconds=300, filename=None):
        params = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl_seconds)


_storage: Storage | None = None


def storage() -> Storage:
    global _storage
    if _storage is None:
        s = settings()
        _storage = S3Storage() if s.storage_backend == "s3" else LocalStorage(s.storage_dir)
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None
