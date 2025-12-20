#!/usr/bin/env python3
"""
Merged, robust worker for MinA (production-ready, defensive).
Combines: media download (with Twilio auth), transcription (with conversion fallback),
language detection, persona-aware routing -> billing/task/clarify,
task extraction, DB persistence and safe stubs for missing modules.

References:
- original file A (longer production file). :contentReference[oaicite:2]{index=2}
- original file B (routing-first trimmed file). :contentReference[oaicite:3]{index=3}
"""

import os
import sys
import time
import json
import tempfile
import traceback
import signal
import subprocess

# HTTP and auth
import requests
from requests.auth import HTTPBasicAuth

# Optional: dotenv load for local dev
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# === Try to import project helpers; provide safe stubs if missing ===

# DB connection helper
try:
    # prefer db.get_conn if present (from file A)
    from db import get_conn
except Exception:
    try:
        # or db_utils.get_conn (from file B)
        from db_utils import get_conn
    except Exception:
        # fallback simple stub (non-persistent)
        class DummyCursor:
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def execute(self, *args, **kwargs): print("[STUB DB] execute", args)
            def fetchone(self): return None
            def fetchall(self): return []
        class DummyConn:
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def cursor(self): return DummyCursor()
            def commit(self): pass
        def get_conn():
            return DummyConn()

# WhatsApp sender helper
try:
    from utils import send_whatsapp  # preferred in file A
except Exception:
    try:
        from whatsapp_utils import send_whatsapp  # fallback in file B
    except Exception:
        def send_whatsapp(phone, message):
            print(f"[STUB send_whatsapp] to={phone} msg={message}")

# Transcription helpers
# Prefer OpenAI-based from file A, else fallback to a simple local STT stub
try:
    from openai_client_multilang import transcribe_file_multilang, summarize_text_multilang
    def transcribe_audio(path):
        # wrapper to unify return signature (transcript, language)
        transcript = transcribe_file_multilang(path, language=None)
        return transcript, None
except Exception:
    # fallback to a local STT utils module if available
    try:
        from stt_utils import transcribe as stt_transcribe
        def transcribe_audio(path):
            try:
                transcript, language = stt_transcribe(path)
                return transcript, language
            except Exception as e:
                print(f"[STUB STT] error: {e}")
                return f"[could-not-transcribe:{os.path.basename(path)}]", None
    except Exception:
        # ultimate fallback: return filename
        def transcribe_audio(path):
            print("[WARN] No STT available; returning filename as transcript")
            return f"[could-not-transcribe:{os.path.basename(path)}]", None

# Router import
try:
    from router import route_intent
except Exception as e:
    route_intent = None
    print(f"[WARN] route_intent import failed: {e}")

# Billing plugin (optional)
try:
    import billing_plugin
except Exception:
    billing_plugin = None

# Task extraction helper loader
def extract_tasks_safe(transcript: str, phone: str):
    try:
        from voice_task_extractor import extract_tasks_from_transcript
    except Exception as e:
        print(f"[WARN] voice_task_extractor not available: {e}")
        return None
    try:
        return extract_tasks_from_transcript(transcript, phone)
    except Exception as e:
        print(f"[ERROR] extract_tasks_from_transcript failed: {e}")
        traceback.print_exc()
        return None

# Language detection (merged from file A)
def _detect_language_from_transcript(transcript):
    if not transcript or len(transcript.strip()) < 10:
        return 'en'
    # Devanagari range
    if any('\u0900' <= ch <= '\u097F' for ch in transcript):
        marathi_words = ['आहे', 'होते', 'करतो', 'करते', 'मला', 'तुला']
        if any(w in transcript for w in marathi_words):
            return 'mr'
        return 'hi'
    # Tamil
    if any('\u0B80' <= ch <= '\u0BFF' for ch in transcript):
        return 'ta'
    # Telugu
    if any('\u0C00' <= ch <= '\u0C7F' for ch in transcript):
        return 'te'
    # Bengali
    if any('\u0980' <= ch <= '\u09FF' for ch in transcript):
        return 'bn'
    # Gujarati
    if any('\u0A80' <= ch <= '\u0AFF' for ch in transcript):
        return 'gu'
    # Kannada
    if any('\u0C80' <= ch <= '\u0CFF' for ch in transcript):
        return 'kn'
    # Punjabi
    if any('\u0A00' <= ch <= '\u0A7F' for ch in transcript):
        return 'pa'
    english_words = ['the', 'and', 'is', 'to', 'of', 'in', 'for', 'with', 'on', 'at']
    english_count = sum(1 for w in english_words if w in transcript[:500].lower())
    if english_count >= 3:
        return 'en'
    return 'hi'

# Graceful shutdown handler (kept for completeness)
class GracefulKiller:
    def __init__(self):
        self.kill_now = False
        try:
            signal.signal(signal.SIGTERM, self._handle_signal)
        except Exception:
            pass
    def _handle_signal(self, signum, frame):
        self.kill_now = True
killer = GracefulKiller()

# ---------------------------
# Main worker function
# ---------------------------
def process_audio_job(meeting_id, media_url):
    """
    End-to-end handler:
    - download media (authenticated for Twilio)
    - transcribe (with fallback / conversion attempts)
    - detect language
    - persist transcript (best-effort)
    - route via persona-aware router -> billing/task/clarify
    - handle each route (billing stub / task extraction / clarification)
    """
    print("🚨 WORKER: ENTERED process_audio_job — NEW CODE ACTIVE")
    # ensure route variable exists in all paths
    route = "task"

    phone = None
    transcript = ""
    detected_language = None
    tmp_path = None
    resp = None

    try:
        print(f"PRODUCTION WORKER: Processing meeting_id={meeting_id} media_url={media_url}")

        # === Fetch phone/audio metadata from DB if available
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT phone, audio_file FROM meeting_notes WHERE id=%s", (meeting_id,))
                row = cur.fetchone()
                if row:
                    phone = row[0] if isinstance(row, (list, tuple)) else getattr(row, 'phone', None) or row[0]
                    audio_url = media_url or (row[1] if isinstance(row, (list, tuple)) else getattr(row, 'audio_file', None))
                else:
                    audio_url = media_url
        except Exception as db_meta_err:
            print(f"[WARN] Could not load meeting metadata: {db_meta_err}")
            audio_url = media_url

        # === Download media (support Twilio auth)
        tmp_path = None
        if audio_url and isinstance(audio_url, str) and audio_url.startswith("http"):
            try:
                twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID")
                twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
                auth = None
                if "twilio.com" in audio_url and twilio_sid and twilio_token:
                    auth = HTTPBasicAuth(twilio_sid, twilio_token)
                    print("[WORKER] Using Twilio HTTP Basic Auth for media download")
                resp = requests.get(audio_url, auth=auth, timeout=60)
                if resp.status_code == 401:
                    raise ValueError(f"Twilio auth failed (401). Check credentials.")
                resp.raise_for_status()
                # Determine likely suffix from content-type
                ctype = resp.headers.get('Content-Type', '').lower()
                if any(x in ctype for x in ['m4a', 'mp4', 'aac']): suffix = '.m4a'
                elif 'wav' in ctype: suffix = '.wav'
                elif any(x in ctype for x in ['ogg','opus']): suffix = '.ogg'
                elif 'webm' in ctype: suffix = '.webm'
                elif 'flac' in ctype: suffix = '.flac'
                else: suffix = '.mp3'
                # Save to temp file
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                    tf.write(resp.content)
                    tmp_path = tf.name
                print(f"WORKER: Saved audio file: {tmp_path}, size: {os.path.getsize(tmp_path)} bytes")
            except Exception as e:
                print(f"[WARN] media download failed: {e}")
                # If webhook provided a local path instead of URL, try it
                if audio_url and os.path.exists(audio_url):
                    tmp_path = audio_url
                    print(f"[WORKER] Using local path media: {tmp_path}")
                else:
                    print("[WARN] No media available to transcribe; exiting job.")
                    return
        else:
            # media_url may already be a local path
            if media_url and os.path.exists(media_url):
                tmp_path = media_url
            else:
                print("[WARN] No media path provided or file does not exist; exiting job.")
                return

        # Basic sanity checks on downloaded file
        try:
            file_size = os.path.getsize(tmp_path)
            if file_size < 128:
                send_whatsapp(phone or "unknown", "⚠️ Audio file too small / corrupt. Please try again.")
                raise ValueError("Audio file too small or corrupt.")
        except Exception as e:
            print(f"[WARN] file sanity check failed: {e}")
            # cleanup and exit
            if tmp_path and os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except: pass
            return

        # Optional proactive transcode (only if configured)
        try:
            proactive_transcode = os.getenv("PROACTIVE_TRANSCODE", "false").lower() == "true"
            if proactive_transcode:
                print("[WORKER] Proactive transcode enabled")
                try:
                    wav_path = tmp_path.rsplit('.', 1)[0] + '.wav'
                    subprocess.run(['ffmpeg','-i', tmp_path, '-ar','16000','-ac','1', wav_path, '-y'],
                                   capture_output=True, text=True, timeout=30)
                    if os.path.exists(wav_path):
                        os.remove(tmp_path)
                        tmp_path = wav_path
                except Exception as e:
                    print(f"[WARN] proactive conversion failed: {e}")
        except Exception:
            pass

        # === Transcription (with conversion retry)
        print(f"PRODUCTION WORKER: Starting transcription for file: {tmp_path}")
        try:
            transcript, detected_language_hint = transcribe_audio(tmp_path)
            if not transcript or len(transcript.strip()) < 5:
                raise ValueError("Transcription empty or too short")
            print(f"PRODUCTION WORKER: Transcription complete, length: {len(transcript)}")
        except Exception as trans_err:
            print(f"[WARN] Initial transcription failed: {trans_err}")
            # Try format conversion and retry once
            try:
                wav_retry = tmp_path.rsplit('.', 1)[0] + '_retry.wav'
                subprocess.run(['ffmpeg','-i', tmp_path, '-acodec','pcm_s16le','-ar','16000','-ac','1', wav_retry, '-y'],
                               capture_output=True, text=True, timeout=35)
                if os.path.exists(wav_retry):
                    try:
                        transcript, detected_language_hint = transcribe_audio(wav_retry)
                        tmp_path = wav_retry
                        if not transcript or len(transcript.strip()) < 5:
                            raise ValueError("Transcription still failed after conversion")
                        print(f"PRODUCTION WORKER: Transcription after conversion complete, length: {len(transcript)}")
                    except Exception as t2:
                        print(f"[WARN] Transcription retry failed: {t2}")
                        raise trans_err
                else:
                    raise trans_err
            except Exception:
                raise trans_err

        # === Detect language
        detected_language = _detect_language_from_transcript(transcript)
        print(f"PRODUCTION WORKER: Detected language: {detected_language}")

        # === Estimate duration (best-effort)
        minutes = 1.0
        try:
            from mutagen import File as MutagenFile
            mf = MutagenFile(tmp_path)
            duration_seconds = float(mf.info.length) if (mf and hasattr(mf, 'info') and hasattr(mf.info, 'length')) else (len(resp.content) * 8) / 80000
            minutes = round(duration_seconds / 60.0, 2)
        except Exception as e:
            print(f"[WARN] duration calc failed: {e}")
            minutes = 1.0

        # === Persist transcript (best-effort)
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    UPDATE meeting_notes
                    SET transcript=%s, detected_language=%s, job_state=%s
                    WHERE id=%s
                """, (transcript, detected_language, 'awaiting_language_choice', meeting_id))
                # optional credits deduction
                try:
                    cur.execute("SELECT credits_remaining, subscription_active FROM users WHERE phone=%s", (phone,))
                    user_row = cur.fetchone()
                    if user_row and not user_row[1]:
                        new_credits = max(0.0, float(user_row[0] or 0.0) - minutes)
                        cur.execute("UPDATE users SET credits_remaining=%s WHERE phone=%s", (new_credits, phone))
                except Exception as e_inner:
                    print(f"[WARN] user credits update failed: {e_inner}")
                conn.commit()
        except Exception as e:
            print(f"[WARN] saving transcript failed: {e}")

        # === Routing decision (after transcription)
        print("🧭 ROUTER: deciding route")
        try:
            if "invoice" in transcript.lower() or "इन्वो" in transcript.lower():
                intent_guess = "create_invoice"
            else:
                intent_guess = "create_task"
            persona = None  # future: load persona from user profile
            if route_intent:
                route = route_intent(intent_guess, persona)
            else:
                # fallback simple mapping
                route = "billing" if intent_guess == "create_invoice" else "task"
            print(f"🧭 ROUTER: intent={intent_guess} persona={persona} route={route}")
        except Exception as e:
            print(f"[WARN] router decision failed: {e}")
            route = "task"

        if route not in ("billing", "task", "clarify"):
            print(f"[WARN] unexpected route '{route}', defaulting to 'task'")
            route = "task"

        # === Execute route (each branch has its own return to avoid fallthrough)
        if route == "billing":
            print("[BILLING] Billing intent detected")
            try:
                if billing_plugin and hasattr(billing_plugin, "handle"):
                    entities = {"transcript": transcript, "meeting_id": meeting_id}
                    context = {"phone": phone, "language": detected_language}
                    billing_plugin.handle("create_invoice", entities, context)
                else:
                    send_whatsapp(phone or "unknown", "🧾 Invoice detected. Billing flow will be available soon.")
            except Exception as be:
                print(f"[ERROR] billing_plugin failed: {be}")
                traceback.print_exc()
                try:
                    send_whatsapp(phone or "unknown", "⚠️ Billing flow encountered an error. Try again later.")
                except Exception:
                    pass
            return {"route": "billing", "handled": True}

        if route == "task":
            print("📋 TASK ROUTE: extracting tasks from transcript")
            try:
                tasks = extract_tasks_safe(transcript, phone)
                if tasks and len(tasks) > 0:
                    task_list = "\n".join([f"{i+1}. {t.get('title', 'Untitled')}" for i, t in enumerate(tasks[:5])])
                    if len(tasks) > 5:
                        task_list += f"\n...and %d more" % (len(tasks) - 5)
                    try:
                        send_whatsapp(phone or "unknown", f"✅ Extracted {len(tasks)} task(s):\n\n{task_list}")
                    except Exception:
                        print("[WARN] send_whatsapp failed for tasks message")
                    print(f"WORKER: Successfully extracted and created {len(tasks)} tasks")
                else:
                    print("WORKER: No tasks found in transcript")
                return {"route": "task", "tasks_count": len(tasks) if tasks else 0}
            except Exception as task_e:
                print(f"[ERROR] task extraction failed: {task_e}")
                traceback.print_exc()
                try:
                    send_whatsapp(phone or "unknown", "⚠️ Task extraction encountered an error. Your transcript is saved.")
                except Exception:
                    pass
                return {"route": "task", "error": str(task_e)}

        if route == "clarify":
            try:
                send_whatsapp(phone or "unknown",
                              "Aap invoice banana chahte ho ya sirf reminder?\n\n1️⃣ Invoice\n2️⃣ Reminder")
            except Exception:
                print("[WARN] failed to send clarify message")
            return {"route": "clarify", "handled": True}

        # unreachable, but safe fallback
        print(f"[WARN] reached unexpected fallback with route={route}")
        return {"route": route}

    except Exception as e_outer:
        print(f"PRODUCTION WORKER: Error during process_audio_job: {e_outer}")
        traceback.print_exc()
        try:
            send_whatsapp(phone or "unknown", "⚠️ Kuch gadbad ho gayi. Dobara try karein.")
        except Exception:
            pass
        return {"error": str(e_outer)}

    finally:
        # cleanup temporary file if created by this job
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
                print(f"WORKER: Cleaned up {tmp_path}")
        except Exception as cleanup_e:
            print(f"[WARN] cleanup failed: {cleanup_e}")


# ---------------------------
# Helper job: complete_summary_job (adapted from file A)
# ---------------------------
def complete_summary_job(meeting_id, chosen_language):
    print(f"COMPLETING SUMMARY: meeting_id={meeting_id}, language={chosen_language}")
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT phone, transcript FROM meeting_notes WHERE id=%s", (meeting_id,))
            row = cur.fetchone()
            if not row:
                return {"error": "Meeting not found"}
            phone = row[0] if isinstance(row, (list, tuple)) else getattr(row, 'phone', None) or row[0]
            transcript = row[1] if isinstance(row, (list, tuple)) else getattr(row, 'transcript', None) or row[1]
            if not transcript:
                return {"error": "No transcript found"}
            # Summarize (use summarize_text_multilang if available)
            try:
                if 'summarize_text_multilang' in globals():
                    summary = summarize_text_multilang(transcript, chosen_language)
                else:
                    # heuristic fallback: return first 400 chars
                    summary = transcript[:400] + ("..." if len(transcript) > 400 else "")
                if not summary or len(summary.strip()) < 10:
                    raise ValueError("Summary generation empty")
            except Exception as sum_e:
                print(f"[WARN] summary generation failed: {sum_e}")
                raise
            # Send and persist
            try:
                lang_name = chosen_language
                formatted_summary = f"Meeting Summary ({lang_name}):\n\n{summary}"
                send_whatsapp(phone, formatted_summary)
            except Exception:
                print("[WARN] failed to send summary message")
            cur.execute("""
                UPDATE meeting_notes
                SET summary=%s, chosen_language=%s, job_state=%s, summary_generated_at=now()
                WHERE id=%s
            """, (summary, chosen_language, 'completed', meeting_id))
            conn.commit()
            return {"success": True}
    except Exception as e:
        print(f"[ERROR] complete_summary_job failed: {e}")
        traceback.print_exc()
        try:
            send_whatsapp(phone or "unknown", "Summary generation failed. Please try again.")
        except Exception:
            pass
        return {"error": str(e)}

# ---------------------------
# Utility job: extract tasks directly (kept for backward compatibility)
# ---------------------------
def extract_tasks_from_voice_job(meeting_id):
    print(f"EXTRACTING TASKS: meeting_id={meeting_id}")
    try:
        from voice_task_extractor import extract_tasks_from_transcript
    except Exception as e:
        print(f"[ERROR] voice_task_extractor import failed: {e}")
        return {"error": str(e)}
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT phone, transcript FROM meeting_notes WHERE id=%s", (meeting_id,))
            row = cur.fetchone()
            if not row:
                return {"error": "Meeting not found"}
            phone = row[0] if isinstance(row, (list, tuple)) else getattr(row, 'phone', None) or row[0]
            transcript = row[1] if isinstance(row, (list, tuple)) else getattr(row, 'transcript', None) or row[1]
        tasks = extract_tasks_from_transcript(transcript, phone)
        if tasks:
            try:
                send_whatsapp(phone, f"✅ Extracted {len(tasks)} task(s) from your voice note!")
            except Exception:
                pass
        else:
            try:
                send_whatsapp(phone, "No tasks found in this voice note.")
            except Exception:
                pass
        return {"success": True, "tasks_count": len(tasks) if tasks else 0}
    except Exception as e:
        print(f"[ERROR] extract_tasks_from_voice_job failed: {e}")
        traceback.print_exc()
        return {"error": str(e)}

# ---------------------------
# Simple test helper
# ---------------------------
def test_worker_job():
    print("PRODUCTION MULTILANG WORKER TEST SUCCESS!")
    return "test_success"

# End of file
