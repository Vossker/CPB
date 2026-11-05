import hashlib
import io
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import BinaryIO, Protocol

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from cpb.config import get_settings
from cpb.errors import NotFoundError, VerificationError


@dataclass(frozen=True)
class StoredObject:
    key: str
    sha256: str
    size_bytes: int


class ObjectStorage(Protocol):
    def put_stream(self, key: str, stream: BinaryIO, max_bytes: int) -> StoredObject: ...

    def stat(self, key: str) -> StoredObject: ...

    def download_url(self, key: str, expires_seconds: int = 900) -> str: ...


def safe_filename(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]", "_", name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    return value[:180] or "object"


def bundle_key(user_id: uuid.UUID, filename: str) -> str:
    return f"users/{user_id}/bundles/{uuid.uuid4()}/{safe_filename(filename)}"


def run_upload_key(run_id: uuid.UUID, kind: str, filename: str) -> str:
    return f"runs/{run_id}/{kind}/{uuid.uuid4()}/{safe_filename(filename)}"


class S3Storage:
    def __init__(self) -> None:
        settings = get_settings()
        common = {
            "aws_access_key_id": settings.storage_access_key,
            "aws_secret_access_key": settings.storage_secret_key,
            "region_name": settings.storage_region,
            "config": Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        }
        self.bucket = settings.storage_bucket
        self.client = boto3.client("s3", endpoint_url=settings.storage_endpoint, **common)
        self.public_client = boto3.client(
            "s3", endpoint_url=settings.storage_public_endpoint, **common
        )

    def put_stream(self, key: str, stream: BinaryIO, max_bytes: int) -> StoredObject:
        digest = hashlib.sha256()
        size = 0
        buffered = io.SpooledTemporaryFile(max_size=min(max_bytes, 8 * 1024 * 1024))
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise VerificationError(f"object exceeds maximum size of {max_bytes} bytes")
            digest.update(chunk)
            buffered.write(chunk)
        buffered.seek(0)
        self.client.upload_fileobj(buffered, self.bucket, key)
        return StoredObject(key=key, sha256=digest.hexdigest(), size_bytes=size)

    def stat(self, key: str) -> StoredObject:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                raise NotFoundError("stored object does not exist") from exc
            raise
        metadata = response.get("Metadata", {})
        sha256 = metadata.get("sha256", "")
        if not sha256:
            # Objects are always read and hashed by the API before acceptance. This marker
            # keeps stat cheap while the verifier uses the server-observed upload digest.
            sha256 = metadata.get("x-amz-meta-sha256", "")
        return StoredObject(key=key, sha256=sha256, size_bytes=response["ContentLength"])

    def tag_sha256(self, stored: StoredObject) -> None:
        self.client.copy_object(
            Bucket=self.bucket,
            Key=stored.key,
            CopySource={"Bucket": self.bucket, "Key": stored.key},
            Metadata={"sha256": stored.sha256},
            MetadataDirective="REPLACE",
        )

    def download_url(self, key: str, expires_seconds: int = 900) -> str:
        return self.public_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )


@lru_cache
def get_storage() -> S3Storage:
    return S3Storage()
