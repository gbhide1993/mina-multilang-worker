# openai_client.py
import os
import subprocess
from openai import OpenAI
from typing import Optional

# Initialize OpenAI client (modern syntax)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Configure models per environment
TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
SUMMARIZE_MODEL = os.getenv("OPENAI_SUMMARIZE_MODEL", "gpt-4o-mini")

def transcribe_file(file_path: str, language: Optional[str]=None) -> str:
    """
    Transcribe audio file to text using OpenAI SDK.
    Returns plain transcript string.
    """
    file_ext = file_path.lower().split('.')[-1]
    converted_path = None
    
    # Convert problematic formats to WAV for better compatibility
    if file_ext in ['ogg', 'opus', 'webm']:
        converted_path = file_path.replace(f'.{file_ext}', '.wav')
        try:
            # Convert to WAV using ffmpeg (if available) or fallback
            result = subprocess.run(['ffmpeg', '-i', file_path, '-acodec', 'pcm_s16le', '-ar', '16000', converted_path, '-y'], 
                                  capture_output=True, text=True)
            if result.returncode == 0:
                file_path = converted_path
            else:
                print(f"FFmpeg conversion failed: {result.stderr}")
                # Try without conversion
                pass
        except FileNotFoundError:
            print("FFmpeg not available, trying original file")
            pass
    
    try:
        with open(file_path, "rb") as f:
            response = client.audio.transcriptions.create(
                model=TRANSCRIBE_MODEL,
                file=f,
                language=language
            )
        result = response.text
        
        # Clean up converted file
        if converted_path and os.path.exists(converted_path):
            os.remove(converted_path)
        return result
        
    except Exception as e:
        print(f"Transcription failed: {e}")
        # Clean up on error
        if converted_path and os.path.exists(converted_path):
            os.remove(converted_path)
        raise

def summarize_text(text: str, instructions: str = "", max_tokens: int = 800, temperature: float = 0.1, language_code: str = "hi") -> str:
    """
    Return a structured summary for meeting text.
    """
    from language_handler import get_summary_instructions
    lang_instruction = get_summary_instructions(language_code)
    
    prompt = f"""You are a professional meeting summarizer. Create comprehensive meeting minutes.

Language Instructions: {lang_instruction}
Additional Instructions: {instructions}

Meeting Content:
{text}

Provide a well-structured summary with:
- Key discussion points
- Important decisions made
- Action items with owners (if mentioned)
- Next steps

Format as clear, readable text (not JSON).
"""
    
    try:
        response = client.chat.completions.create(
            model=SUMMARIZE_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert meeting summarizer who creates clear, actionable meeting minutes."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=max_tokens,
            temperature=temperature
        )
        summary = response.choices[0].message.content.strip()
        # Truncate to fit WhatsApp 1600 char limit with buffer
        return summary[:1500] + "..." if len(summary) > 1500 else summary
    except Exception as e:
        print(f"Summarization failed: {e}")
        raise
