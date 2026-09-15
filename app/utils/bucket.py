from app.settings.config import settings
import boto3
import os
from botocore.exceptions import NoCredentialsError
from uuid import uuid4
from fastapi import HTTPException, UploadFile
import logging

logger = logging.getLogger(__name__)

AWS_ACCESS_KEY = settings.AWS_ACCESS_KEY
AWS_SECRET_KEY = settings.AWS_SECRET_KEY
AWS_BUCKET_NAME = settings.AWS_BUCKET_NAME
AWS_REGION = settings.AWS_REGION

s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

async def upload_to_s3(file: UploadFile) -> str:
    """Standard upload (used for avatars/general assets)."""
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded")
    file_extension = os.path.splitext(file.filename)[1]
    unique_filename = f"public/{uuid4()}{file_extension}"
    try:
        # Uploading without explicit ACL as many modern buckets disable them
        s3_client.upload_fileobj(
            file.file,
            AWS_BUCKET_NAME,
            unique_filename
        )
        file_url = f"https://{AWS_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{unique_filename}"
        return file_url
    except Exception as e:
        logger.error(f"S3 Upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def upload_secure_file(file: UploadFile, folder: str = "passports") -> str:
    """Secure upload with Server-Side Encryption and no public ACL."""
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded")
    
    file_extension = os.path.splitext(file.filename)[1].lower()
    unique_filename = f"{folder}/{uuid4()}{file_extension}"
    
    try:
        # Use AES256 for Encryption at Rest
        s3_client.upload_fileobj(
            file.file,
            AWS_BUCKET_NAME,
            unique_filename,
            ExtraArgs={
                'ServerSideEncryption': 'AES256',
                'ContentType': file.content_type
            }
        )
        
        # We return the KEY, not a URL, because these files are PRIVATE.
        # Pre-signed URLs should be generated only upon authorized request.
        return unique_filename
    except Exception as e:
        logger.error(f"Secure S3 Upload failed: {e}")
        raise HTTPException(status_code=500, detail="Secure upload failed")

def generate_presigned_url(file_key: str, expiration: int = 3600) -> str:
    """Generates a temporary URL for viewing private files."""
    try:
        response = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': AWS_BUCKET_NAME, 'Key': file_key},
            ExpiresIn=expiration
        )
        return response
    except Exception as e:
        logger.error(f"Failed to generate pre-signed URL: {e}")
        return None
