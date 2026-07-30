"""S3 bytes backend for the files primitive.

When ``RYA_FILES_S3_BUCKET`` is set, file BYTES live in S3 and only metadata
stays in the store (rya_files / .rya/files). Same files API, same ctx.files -
handlers never know the difference. Large uploads can bypass the API entirely
via presigned PUT URLs (POST /files/presign -> browser PUTs to S3 -> POST
/files/{id}/confirm fires file.uploaded).
"""

from __future__ import annotations

import os
from typing import Optional


def bucket() -> Optional[str]:
    return os.environ.get("RYA_FILES_S3_BUCKET") or None


def _client():
    import boto3
    region = (os.environ.get("RYA_BEDROCK_REGION") or os.environ.get("AWS_REGION")
              or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    kwargs: dict = {"region_name": region}
    # Deliberately duplicated from bundles._s3_client rather than shared: that
    # module is in the client SDK's surface (packaging/surface.py) and this one is
    # platform-only, so a common helper would drag one into the other's wheel.
    # Same rationale, same three lines — keep them in step.
    endpoint = (os.environ.get("RYA_FILES_S3_ENDPOINT") or "").strip()
    if endpoint:
        from botocore.config import Config
        kwargs["endpoint_url"] = endpoint
        kwargs["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", **kwargs)


def key_for(file_id: str) -> str:
    return f"files/{file_id}"


def put_bytes(file_id: str, content: bytes, content_type: str) -> None:
    _client().put_object(Bucket=bucket(), Key=key_for(file_id), Body=content,
                         ContentType=content_type)


def get_bytes(file_id: str) -> Optional[bytes]:
    try:
        return _client().get_object(Bucket=bucket(), Key=key_for(file_id))["Body"].read()
    except Exception:
        return None


def head(file_id: str) -> Optional[dict]:
    try:
        h = _client().head_object(Bucket=bucket(), Key=key_for(file_id))
        return {"size": h["ContentLength"], "contentType": h.get("ContentType")}
    except Exception:
        return None


def presign_put(file_id: str, content_type: str, expires: int = 900) -> str:
    return _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket(), "Key": key_for(file_id), "ContentType": content_type},
        ExpiresIn=expires)
