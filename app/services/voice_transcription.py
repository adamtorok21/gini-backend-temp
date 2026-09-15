import os
import tempfile
import logging
from typing import Optional
from openai import OpenAI
from app.settings.config import settings

logger = logging.getLogger(__name__)

class VoiceTranscriptionService:
    """Service for transcribing voice notes using OpenAI Whisper"""
    
    def __init__(self):
        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)
        
    async def transcribe_audio(self, audio_data: bytes, filename: str = "voice.ogg") -> Optional[str]:
        """
        Transcribe audio data using OpenAI Whisper API
        
        Args:
            audio_data: Binary audio data
            filename: Original filename for format detection
            
        Returns:
            Transcribed text or None if failed
        """
        try:
            # Create temporary file
            with tempfile.NamedTemporaryFile(delete=False, suffix=self._get_file_extension(filename)) as temp_file:
                temp_file.write(audio_data)
                temp_file_path = temp_file.name
            
            try:
                # Transcribe using OpenAI Whisper
                with open(temp_file_path, "rb") as audio_file:
                    # No language param — Whisper auto-detects (supports 99 languages)
                    transcript = self.client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                        response_format="text"
                    )
                
                logger.info(f"Successfully transcribed audio: {len(transcript)} characters")
                return transcript
                
            finally:
                # Clean up temporary file
                os.unlink(temp_file_path)
                
        except Exception as e:
            logger.error(f"Voice transcription failed: {str(e)}")
            return None
    
    def _get_file_extension(self, filename: str) -> str:
        """Get appropriate file extension based on filename"""
        filename_lower = filename.lower()
        if filename_lower.endswith('.mp3'):
            return '.mp3'
        elif filename_lower.endswith('.wav'):
            return '.wav'
        elif filename_lower.endswith('.m4a'):
            return '.m4a'
        elif filename_lower.endswith('.ogg'):
            return '.ogg'
        else:
            return '.ogg'  # Default for WhatsApp voice notes
    
    async def transcribe_whatsapp_voice(self, media_url: str) -> Optional[str]:
        """
        Download and transcribe WhatsApp voice note
        
        Args:
            media_url: WhatsApp media URL for voice note
            
        Returns:
            Transcribed text or None if failed
        """
        try:
            import httpx
            
            # Download audio from WhatsApp
            async with httpx.AsyncClient() as client:
                response = await client.get(media_url, timeout=30.0)
                response.raise_for_status()
                audio_data = response.content
            
            # Transcribe the downloaded audio
            return await self.transcribe_audio(audio_data, "whatsapp_voice.ogg")
            
        except Exception as e:
            logger.error(f"WhatsApp voice transcription failed: {str(e)}")
            return None

# Global instance
voice_service = VoiceTranscriptionService()

async def transcribe_voice_note(audio_data: bytes, filename: str = "voice.ogg") -> Optional[str]:
    """Convenience function for voice transcription"""
    return await voice_service.transcribe_audio(audio_data, filename)

async def transcribe_whatsapp_voice_note(media_url: str) -> Optional[str]:
    """Convenience function for WhatsApp voice transcription"""
    return await voice_service.transcribe_whatsapp_voice(media_url)
