import boto3
import asyncio
from concurrent.futures import ThreadPoolExecutor
from botocore.exceptions import ClientError
import uuid
from datetime import datetime, timedelta
import os
import tempfile
from app.settings.config import settings

class S3Service:
    def __init__(self):
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=settings.AWS_ACCESS_KEY,
            aws_secret_access_key=settings.AWS_SECRET_KEY,
            region_name=settings.AWS_REGION
        )
        self.bucket_name = settings.AWS_BUCKET_NAME
        self.executor = ThreadPoolExecutor()

    async def upload_file_async(self, file_path: str, object_key: str, expires_in: int = 86400) -> dict:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                self.executor,
                self._upload_file_sync,
                file_path,
                object_key
            )
            
            # Generate presigned URL for download
            download_url = self.s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.bucket_name, 'Key': object_key},
                ExpiresIn=expires_in  # URL expires in 24 hours by default
            )
            
            return {
                'success': True,
                'download_url': download_url,
                'object_key': object_key,
                'expires_at': datetime.now() + timedelta(seconds=expires_in)
            }
            
        except ClientError as e:
            print(f"S3 upload error: {e}")
            return {'success': False, 'error': str(e)}
        except Exception as e:
            print(f"Unexpected error: {e}")
            return {'success': False, 'error': str(e)}

    def _upload_file_sync(self, file_path: str, object_key: str):
        """Synchronous file upload"""
        self.s3_client.upload_file(
            file_path,
            self.bucket_name,
            object_key,
            ExtraArgs={'ContentType': 'application/pdf'}
        )

    def generate_download_url(self, object_key: str, expires_in: int = 3600) -> str:
        """Regenerate a fresh presigned download URL for an already-uploaded
        invoice object. Used by the permanent /invoice/{order}/download proxy
        endpoint so the guest-facing receipt link never expires (the endpoint
        mints a new short-lived S3 link on each request; the bucket stays
        private)."""
        return self.s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.bucket_name, 'Key': object_key},
            ExpiresIn=expires_in,
        )

# Initialize S3 service
s3_service = S3Service()