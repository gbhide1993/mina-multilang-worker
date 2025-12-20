#!/usr/bin/env python3
# worker_multilang_production_fixed_clean.py
# MinA - Worker (fixed routing + billing stub + task extraction)
# Replace your existing worker file with this (or merge changes).
# This file assumes certain helper functions/modules exist in your repo:
# - send_whatsapp(phone, message)
# - get_conn() -> context manager for DB connection
# - transcribe_audio(file_path) -> returns (transcript, detected_language)
# - voice_task_extractor.extract_tasks_from_transcript(transcript, phone)
# - route.py with route_intent(intent, persona)
# - billing_plugin.handle(...) (optional)
#
# The file is defensive: if any helper is missing it logs a helpful message.

import os
import sys
import traceback
import time
from typing import Optional

# Try to import existing helpers; if missing, provide safe stubs with logs
try:
    from router import route_intent
except Exception as e:
    route_intent = None
    print(f"[WARN] could not import route_intent from router.py: {e}")

# Optional billing plugin - not required for routing tests
try:
    import billing_plugin
except Exception:
    billing_plugin = None

# send_whatsapp helper (expected to exist in repo)
try:
    from whatsapp_utils import send_whatsapp  # adapt if module name differs
except Exception:
    # fallback stub - logs only
    def send_whatsapp(phone, message):
        print(f"[STUB send_whatsapp] phone={phone} message={message}")

# get_conn DB helper expected to exist
try:
    from db_utils import get_conn  # adapt if module name differs
except Exception:
    # fallback stub: context manager that does nothing
    class DummyConn:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def cursor(self):
            class C:
                def __enter__(self):
                    return self
                def __exit__(self, exc_type, exc, tb):
                    return False
                def execute(self, *args, **kwargs):
                    print("[STUB DB] execute", args)
                def fetchone(self):
                    return None
            return C()
    def get_conn():
        return DummyConn()

# transcribe_audio expected: implement STT in your repo
def transcribe_audio(file_path: str) -> (str, str):
    """
    Try to call your real STT. If not found, this stub returns the file name as transcript.
    Replace with actual STT component.
    """
    # If you have a real STT module, import and call it here.
    try:
        from stt_utils import transcribe  # adapt to your actual module
        transcript, language = transcribe(file_path)
        return transcript, language
    except Exception as e:
        print(f"[STUB STT] transcribe_audio fallback for {file_path}: {e}")
        # crude fallback: return filename as transcript (NOT FOR PRODUCTION)
        return f"[could-not-transcribe:{os.path.basename(file_path)}]", "unknown"

# Helper to load task extractor dynamically when needed
def extract_tasks_safe(transcript: str, phone: str):
    try:
        from voice_task_extractor import extract_tasks_from_transcript
    except Exception as e:
        print(f"[ERROR] voice_task_extractor not available: {e}")
        return None
    try:
        return extract_tasks_from_transcript(transcript, phone)
    except Exception as e:
        print(f"[ERROR] extract_tasks_from_transcript failed: {e}")
        traceback.print_exc()
        return None

# Main function processing an audio job
def process_audio_job(meeting_id, media_url):
    """
    End-to-end processing of an audio media job:
    - download media (assumed already saved by webhook; adapt as needed)
    - transcribe audio
    - detect language
    - route intent using route_intent(intent, persona)
    - either run billing workflow OR task extraction
    - store transcripts/metadata in DB safely
    """
    print("🚨 WORKER: ENTERED process_audio_job — NEW CODE ACTIVE")

    # SAFE DEFAULT: ensure route exists in all code paths
    route = "task"

    # Basic metadata placeholders (adapt to your actual webhook payload parsing)
    phone = None
    transcript = ""
    detected_language = None
    tmp_path = None

    try:
        print(f"PRODUCTION WORKER: Processing meeting_id={meeting_id} media_url={media_url}")

        # -----------------------
        # Download media (if needed)
        # -----------------------
        # If your webhook already saved file and passed a path rather than URL, use it.
        # Here we attempt to download to a temp file (if URL looks like a remote media).
        tmp_path = None
        if media_url and media_url.startswith("http"):
            # simple download; adapt auth/headers as needed
            try:
                import requests
                r = requests.get(media_url, timeout=10)
                if r.status_code == 200:
                    ext = ".ogg"
                    tmp_path = f"/tmp/mina_media_{int(time.time())}{ext}"
                    with open(tmp_path, "wb") as fh:
                        fh.write(r.content)
                    print(f"WORKER: Saved audio file: {tmp_path}, size: {os.path.getsize(tmp_path)} bytes")
                else:
                    print(f"[WARN] media download returned status {r.status_code}")
            except Exception as e:
                print(f"[WARN] failed to download media: {e}")
        else:
            # If webhook already gave you a path, use it directly
            tmp_path = media_url if media_url and os.path.exists(media_url) else None

        if not tmp_path:
            print("[WARN] No media file available to transcribe; exiting job.")
            return

        # -----------------------
        # Transcription
        # -----------------------
        print(f"PRODUCTION WORKER: Starting transcription for file: {tmp_path}")
        transcript, detected_language = transcribe_audio(tmp_path)
        print(f"PRODUCTION WORKER: Transcription complete, length: {len(transcript)}")
        print(f"PRODUCTION WORKER: Transcript preview: {transcript[:300]}...")
        print(f"PRODUCTION WORKER: Detected language: {detected_language}")

        # -----------------------
        # Save transcript to DB (optional) - do minimal, non-blocking write
        # -----------------------
        try:
            with get_conn() as conn, conn.cursor() as cur:
                # adapt to your DB schema
                try:
                    cur.execute(
                        "INSERT INTO transcripts (meeting_id, transcript, language, created_at) VALUES (%s, %s, %s, NOW())",
                        (meeting_id, transcript, detected_language)
                    )
                    conn.commit()
                except Exception as db_e:
                    print(f"[WARN] could not save transcript to DB: {db_e}")
        except Exception as e:
            print(f"[WARN] DB connection not available or save failed: {e}")

        # -----------------------
        # ROUTER DECISION (after transcription, before task extraction)
        # -----------------------
        print("🧭 ROUTER: deciding route")

        try:
            # Temporary intent guess until you plug a real intent classifier
            if "invoice" in transcript.lower() or "इन्वो" in transcript.lower():
                intent_guess = "create_invoice"
            else:
                intent_guess = "create_task"

            persona = None  # TODO: load from user profile when available

            if route_intent:
                route = route_intent(intent_guess, persona)
            else:
                # fallback default: shopkeeper patterns route to billing if "invoice" present
                route = "billing" if intent_guess == "create_invoice" and persona == "SHOPKEEPER" else ("billing" if intent_guess == "create_invoice" else "task")

            print(f"🧭 ROUTER: intent={intent_guess} persona={persona} route={route}")

        except Exception as e:
            print(f"❌ ROUTER ERROR: {e}")
            traceback.print_exc()
            route = "task"

        # Defensive assertion
        if route not in ("billing", "task", "clarify"):
            print(f"[WARN] Unexpected route '{route}' - defaulting to 'task'")
            route = "task"

        # -----------------------
        # ROUTE EXECUTION (outside DB transaction)
        # -----------------------
        if route == "billing":
            # If billing_plugin exists, call it; otherwise stub acknowledge
            print("[BILLING] Billing intent detected — routing to billing workflow (or stub)")
            try:
                if billing_plugin and hasattr(billing_plugin, "handle"):
                    # Pass structured context as your plugin expects
                    entities = {"transcript": transcript, "meeting_id": meeting_id}
                    context = {"phone": phone, "language": detected_language}
                    billing_plugin.handle("create_invoice", entities, context)
                else:
                    # Temporary user-facing acknowledgement while billing plugin is developed
                    send_whatsapp(phone or "unknown", "🧾 Invoice samjha. Billing flow abhi build ho raha hai.")
            except Exception as b_err:
                print(f"[ERROR] billing_plugin invocation failed: {b_err}")
                traceback.print_exc()
                send_whatsapp(phone or "unknown", "⚠️ Billing flow encountered an error. Try again later.")
            # Important: stop further processing for this message
            return

        elif route == "task":
            # Restore your original task extraction logic here
            print("📋 TASK ROUTE: extracting tasks from transcript")
            tasks = None
            try:
                tasks = extract_tasks_safe(transcript, phone)
                if tasks and len(tasks) > 0:
                    task_list = "\n".join([f"{i+1}. {t.get('title', 'Untitled')}" for i, t in enumerate(tasks[:5])])
                    if len(tasks) > 5:
                        task_list += f"\n...and %d more" % (len(tasks) - 5)
                    send_whatsapp(phone or "unknown", f"✅ Extracted {len(tasks)} task(s):\n\n{task_list}")
                    print(f"WORKER: Successfully extracted and created {len(tasks)} tasks")
                else:
                    print("WORKER: No tasks found in transcript (task extractor returned empty)")
            except Exception as task_error:
                print(f"WORKER: Task extraction FAILED with error: {task_error}")
                traceback.print_exc()
                send_whatsapp(phone or "unknown", "⚠️ Task extraction encountered an error. Your transcript is saved.")
            # After tasks handling, we may return or allow other flows; choose to return to avoid duplicate logic
            return

        elif route == "clarify":
            # Ask a clarification question
            send_whatsapp(phone or "unknown",
                          "Aap invoice banana chahte ho ya sirf reminder?\n\n1️⃣ Invoice\n2️⃣ Reminder")
            return

        else:
            # Should not reach here because of earlier assertion, but safe fallback
            print(f"[WARN] Reached fallback branch with route={route}. No action taken.")
            return

    except Exception as e_outer:
        # Catch-all for unexpected errors in processing
        print(f"PRODUCTION WORKER: Error during process_audio_job: {e_outer}")
        traceback.print_exc()
        try:
            send_whatsapp(phone or "unknown", "⚠️ Kuch gadbad ho gayi. Dobara try karein.")
        except Exception:
            print("[WARN] Failed to send error message via send_whatsapp")
    finally:
        # Clean up temporary file if we created it
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
                print(f"WORKER: Cleaned up {tmp_path}")
        except Exception as cleanup_err:
            print(f"[WARN] failed to cleanup tmp file {tmp_path}: {cleanup_err}")

# If you want a small test that runs locally (for developers), you can call:
if __name__ == "__main__":
    # Example quick test - adapt meeting_id and a media_url/path to an audio file
    test_meeting_id = 9999
    test_media_url = "/tmp/example.ogg"  # replace with real path
    print("Running quick local test for process_audio_job() - adapt media path then run")
    process_audio_job(test_meeting_id, test_media_url)
