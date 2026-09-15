from fastapi import HTTPException, UploadFile
from typing import Any
import qrcode
from io import BytesIO
from uuid import uuid4
from app.utils.bucket import upload_to_s3

async def generate_and_upload_qrcode(data: Any) -> str:
    try:
        # Generate QR code
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        
        # If it's a dict, we might want to convert to a URL or just stringify
        # For guest-facing QRs, this SHOULD be a URL string.
        qr_data = data
        if isinstance(data, dict):
            # If a website_url is provided in the dict, prioritize it
            qr_data = data.get("website_url") or str(data)
            
        qr.add_data(qr_data)
        qr.make(fit=True)
        qr_image = qr.make_image(fill="black", back_color="white")

        # Save QR code to memory
        buffer = BytesIO()
        qr_image.save(buffer, format="PNG")
        buffer.seek(0)

        # Create a SpooledTemporaryFile or BytesIO object
        qr_file = BytesIO(buffer.read())
        qr_file.name = f"qr_code_{uuid4()}.png"  # Assign a filename
        buffer.seek(0)  # Reset buffer position after reading

        # Use the upload_to_s3 function
        qr_code_url = await upload_to_s3(UploadFile(filename=qr_file.name, file=qr_file))
        return qr_code_url
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating QR code: {str(e)}")

