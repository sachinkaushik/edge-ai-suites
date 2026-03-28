# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""
Frame Store — manages camera frames in MinIO object storage.

Buckets:
  frames/      — raw camera frames  {camera}/{timestamp}.jpg
  thumbnails/  — person crops       {object_id}/{camera}/{timestamp}.jpg
  evidence/    — alert evidence      {alert_id}/frame_{idx}.jpg
"""

import base64
import io
from datetime import datetime, timezone
from typing import List, Optional

import structlog

from .config import ConfigService

logger = structlog.get_logger(__name__)

try:
    from minio import Minio
    from minio.error import S3Error
except ImportError:
    Minio = None
    S3Error = Exception
    logger.warning("minio package not installed — FrameStore will be no-op")


class FrameStore:
    """Thin wrapper around MinIO for frame storage and retrieval."""

    def __init__(self, config: ConfigService) -> None:
        minio_cfg = config.get_minio_config()
        self.endpoint = minio_cfg.get("endpoint", "minio:9000")
        self.access_key = minio_cfg.get("access_key", "minioadmin")
        self.secret_key = minio_cfg.get("secret_key", "minioadmin")
        self.secure = minio_cfg.get("secure", False)
        self.bucket_frames = minio_cfg.get("bucket_frames", "frames")
        self.bucket_thumbnails = minio_cfg.get("bucket_thumbnails", "thumbnails")
        self.bucket_evidence = minio_cfg.get("bucket_evidence", "evidence")
        self.retention_hours = minio_cfg.get("retention_hours", 24)

        self.client: Optional["Minio"] = None
        if Minio:
            self.client = Minio(
                self.endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.secure,
            )

        logger.info(
            "FrameStore initialized",
            endpoint=self.endpoint,
            buckets=[self.bucket_frames, self.bucket_thumbnails, self.bucket_evidence],
        )

    async def ensure_buckets(self) -> None:
        """Create buckets if they don't exist."""
        if not self.client:
            return
        for bucket in (self.bucket_frames, self.bucket_thumbnails, self.bucket_evidence):
            try:
                if not self.client.bucket_exists(bucket):
                    self.client.make_bucket(bucket)
                    logger.info("Created bucket", bucket=bucket)
            except S3Error:
                logger.exception("Bucket check/create failed", bucket=bucket)

    # ---- write ---------------------------------------------------------------
    def store_frame(self, camera_id: str, image_bytes: bytes, ts: Optional[datetime] = None) -> str:
        """Write a raw camera frame; return the MinIO object key."""
        ts = ts or datetime.now(timezone.utc)
        key = f"{camera_id}/{ts.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        self._put(self.bucket_frames, key, image_bytes)
        return key

    def store_crop(self, object_id: str, camera_id: str, image_bytes: bytes, ts: Optional[datetime] = None) -> str:
        """Write a person crop (thumbnail); return the MinIO key."""
        ts = ts or datetime.now(timezone.utc)
        key = f"{object_id}/{camera_id}/{ts.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        self._put(self.bucket_thumbnails, key, image_bytes)
        return key

    def store_evidence(self, alert_id: str, idx: int, image_bytes: bytes) -> str:
        """Write an evidence frame for an alert."""
        key = f"{alert_id}/frame_{idx:03d}.jpg"
        self._put(self.bucket_evidence, key, image_bytes)
        return key

    # ---- read ----------------------------------------------------------------
    def get_frame(self, key: str) -> Optional[bytes]:
        """Read frame bytes by key from the frames bucket."""
        return self._get(self.bucket_frames, key)

    def get_crop(self, key: str) -> Optional[bytes]:
        return self._get(self.bucket_thumbnails, key)

    async def get_frames_base64(self, keys: List[str]) -> List[str]:
        """Fetch multiple frames and return base64-encoded strings."""
        results = []
        for key in keys:
            raw = self._get(self.bucket_frames, key)
            if raw is None:
                raw = self._get(self.bucket_thumbnails, key)
            if raw:
                results.append(base64.b64encode(raw).decode("ascii"))
        return results

    # ---- internal helpers ----------------------------------------------------
    def _put(self, bucket: str, key: str, data: bytes) -> None:
        if not self.client:
            return
        try:
            self.client.put_object(
                bucket, key, io.BytesIO(data), length=len(data),
                content_type="image/jpeg",
            )
        except S3Error:
            logger.exception("MinIO put failed", bucket=bucket, key=key)

    def _get(self, bucket: str, key: str) -> Optional[bytes]:
        if not self.client:
            return None
        try:
            resp = self.client.get_object(bucket, key)
            return resp.read()
        except S3Error:
            logger.debug("MinIO get miss", bucket=bucket, key=key)
            return None
        finally:
            try:
                resp.close()
                resp.release_conn()
            except Exception:
                pass
