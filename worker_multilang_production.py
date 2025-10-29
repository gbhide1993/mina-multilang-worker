#!/usr/bin/env python3
"""
Production Multilang Worker - Implementing Best Practices
- Configurable timeout with exponential backoff
- Separate DB columns for clean state management
- Idempotent operations with proper cleanup
- Consistent summarizer design
"""

import os
import tempfile
import requests
import traceback
import json
import time
import signal
from dotenv import load_dotenv

load_dotenv()

from db import get_conn
from utils import send_whatsapp
from openai_client_multilang import transcribe_file_multilang, summarize_text_multilang
from language_handler_v2 import get_language_menu, get_language_name

# Configuration
LANG_CHOICE_TIMEOUT = int(os.getenv("LANG_CHOICE_TIMEOUT", "45"))
MAX_BACKOFF = 8  # seconds

class GracefulKiller:
    """Handle SIGTERM for graceful shutdown"""
    def __init__(self):
        self.kill_now = False
        signal.signal(signal.SIGTERM, self._handle_signal)
    
    def _handle_signal(self, signum, frame):
        self.kill_now = True

killer = GracefulKiller()

def _detect_language_from_transcript(transcript):
    """Detect language from transcript text"""
    if not transcript or len(transcript.strip()) < 10:
        return 'en'
    
    # Check for Devanagari script (Hindi/Marathi)
    if any('\u0900' <= char <= '\u097F' for char in transcript):
        marathi_words = ['आहे', 'होते', 'करतो', 'करते', 'मला', 'तुला']
        if any(word in transcript for word in marathi_words):
            return 'mr'
        return 'hi'
    
    # Check for other scripts
    if any('\u0B80' <= char <= '\u0BFF' for char in transcript):
        return 'ta'
    if any('\u0C00' <= char <= '\u0C7F' for char in transcript):
        return 'te'
    if any('\u0980' <= char <= '\u09FF' for char in transcript):
        return 'bn'
    if any('\u0A80' <= char <= '\u0AFF' for char in transcript):
        return 'gu'
    if any('\u0C80' <= char <= '\u0CFF' for char in transcript):
        return 'kn'
    if any('\u0A00' <= char <= '\u0A7F' for char in transcript):
        return 'pa'
    
    # Check for English
    english_words = ['the', 'and', 'is', 'to', 'of', 'in', 'for', 'with', 'on', 'at']
    english_count = sum(1 for word in english_words if word in transcript[:500].lower())
    
    if english_count >= 3:
        return 'en'
    
    return 'hi'  # Default to Hindi

def _wait_for_language_choice(phone, meeting_id, timeout=LANG_CHOICE_TIMEOUT):
    """Wait for language choice with exponential backoff"""
    print(f"🌐 WORKER: Waiting for language choice (timeout: {timeout}s)")
    
    start_time = time.time()
    backoff = 1
    
    while time.time() - start_time < timeout and not killer.kill_now:
        try:
            with get_conn() as conn, conn.cursor() as cur:
                # Poll dedicated column instead of JSON parsing
                cur.execute("SELECT chosen_language FROM meeting_notes WHERE phone=%s AND id=%s", (phone, meeting_id))
                row = cur.fetchone()
                
                if row and row[0]:
                    chosen_lang = row[0]
                    print(f"🌐 WORKER: Language selected: {chosen_lang}")
                    return chosen_lang
            
            # Exponential backoff: 1s → 2s → 4s → 8s
            time.sleep(min(backoff, MAX_BACKOFF))
            backoff = min(backoff * 2, MAX_BACKOFF)
            
        except Exception as e:
            print(f"🌐 WORKER: Error checking language choice: {e}")
            time.sleep(2)
    
    if killer.kill_now:
        print("🌐 WORKER: Graceful shutdown requested")
        return None
    
    print(f"🌐 WORKER: Timeout after {timeout}s")
    return None

def process_audio_job(meeting_id, media_url):
    """Process audio and wait for language selection"""
    print(f"🌐 PRODUCTION WORKER: Processing meeting_id={meeting_id}")
    tmp_path = None
    
    try:
        # Get meeting details
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT phone, audio_file FROM meeting_notes WHERE id=%s", (meeting_id,))
            row = cur.fetchone()
            if not row:
                return {"error": "Meeting not found"}
            
            phone = row[0] if hasattr(row, '__getitem__') else row.phone
            audio_url = media_url or (row[1] if hasattr(row, '__getitem__') else row.audio_file)
        
        # Download audio
        auth = None
        twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
        twilio_token = os.getenv("TWILIO_AUTH_TOKEN")
        if "twilio.com" in audio_url and twilio_sid and twilio_token:
            auth = (twilio_sid, twilio_token)
        
        resp = requests.get(audio_url, auth=auth, timeout=60)
        resp.raise_for_status()
        
        # Save to temp file
        content_type = resp.headers.get('Content-Type', '')
        suffix = '.opus' if 'opus' in content_type.lower() else '.mp3'
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name
        
        # Transcribe
        transcript = transcribe_file_multilang(tmp_path, language=None)
        detected_language = _detect_language_from_transcript(transcript)
        
        # Calculate credits
        try:
            from mutagen import File as MutagenFile
            mf = MutagenFile(tmp_path)
            minutes = round(float(mf.info.length) / 60.0, 2) if mf and hasattr(mf, 'info') else 1.0
        except:
            minutes = 1.0
        
        # Store in separate columns (clean state management)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE meeting_notes 
                SET transcript=%s, detected_language=%s, job_state=%s
                WHERE id=%s
            """, (transcript, detected_language, 'awaiting_language_choice', meeting_id))
            
            # Deduct credits
            cur.execute("SELECT credits_remaining, subscription_active FROM users WHERE phone=%s", (phone,))
            user_row = cur.fetchone()
            if user_row and not user_row[1]:  # Not subscribed
                new_credits = max(0.0, float(user_row[0] or 0.0) - minutes)
                cur.execute("UPDATE users SET credits_remaining=%s WHERE phone=%s", (new_credits, phone))
            
            conn.commit()
        
        # Send language menu
        detected_name = get_language_name(detected_language)
        menu = get_language_menu()
        
        message = f"🎙️ *Audio transcribed!*\n🔍 Detected: *{detected_name}*\n\n📝 *Choose summary language:*\n\n{menu}"
        send_whatsapp(phone, message)
        
        # Wait for language choice with configurable timeout
        chosen_language = _wait_for_language_choice(phone, meeting_id, LANG_CHOICE_TIMEOUT)
        
        if not chosen_language:
            # Timeout fallback to English
            chosen_language = 'en'
            send_whatsapp(phone, f"⏰ No response in {LANG_CHOICE_TIMEOUT}s. Using English...")
        
        # Generate summary (consistent design - always use multilang)
        summary = summarize_text_multilang(transcript, chosen_language)
        
        # Idempotent final update with timestamp
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE meeting_notes 
                SET summary=%s, chosen_language=%s, job_state=%s, summary_generated_at=NOW()
                WHERE id=%s AND summary_generated_at IS NULL
            """, (summary, chosen_language, 'completed', meeting_id))
            
            # Check if we actually updated (idempotent)
            if cur.rowcount > 0:
                lang_name = get_language_name(chosen_language)
                header = f"📝 *Meeting Summary ({lang_name}):*\n\n"
                send_whatsapp(phone, header + summary)
                print(f"🌐 WORKER: Summary sent in {lang_name}")
            else:
                print("🌐 WORKER: Summary already generated (idempotent)")
            
            conn.commit()
        
        return {"success": True, "language": chosen_language}
        
    except Exception as e:
        print(f"🌐 WORKER ERROR: {e}")
        traceback.print_exc()
        return {"error": str(e)}
        
    finally:
        # Ensure cleanup in finally block
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
                print(f"🌐 WORKER: Cleaned up {tmp_path}")
            except Exception as e:
                print(f"🌐 WORKER: Cleanup failed: {e}")

def complete_summary_job(meeting_id, chosen_language):
    """Separate job for summary completion (if needed)"""
    print(f"🌐 COMPLETING SUMMARY: meeting_id={meeting_id}, language={chosen_language}")
    
    try:
        with get_conn() as conn, conn.cursor() as cur:
            # Atomic language choice update
            cur.execute("""
                UPDATE meeting_notes 
                SET chosen_language=%s, job_state=%s
                WHERE id=%s AND chosen_language IS NULL
            """, (chosen_language, 'language_selected', meeting_id))
            conn.commit()
            
            if cur.rowcount > 0:
                print(f"🌐 WORKER: Language choice stored: {chosen_language}")
                return {"success": True}
            else:
                print("🌐 WORKER: Language already chosen (idempotent)")
                return {"success": True, "already_set": True}
        
    except Exception as e:
        print(f"🌐 SUMMARY ERROR: {e}")
        return {"error": str(e)}

def test_worker_job():
    print("🧪 PRODUCTION MULTILANG WORKER TEST SUCCESS!")
    return "test_success"