#!/usr/bin/env python3
"""
Ultra Minimal Worker - For very low usage (3-4 users/day)
Optimized for maximum Redis savings
"""
import os
import time
import sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
load_dotenv()

def run_ultra_minimal_worker():
    """Ultra-conservative worker for low usage scenarios"""
    from rq import Worker
    from redis import from_url
    
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        print("ERROR: REDIS_URL not found")
        return
    
    try:
        connection = from_url(redis_url)
        connection.ping()
        print(f"✅ Connected to Redis: {redis_url[:50]}...")
        
        worker = Worker(['default'], connection=connection)
        
        print("🔧 Starting ultra-minimal worker (5-minute intervals)...")
        consecutive_empty = 0
        
        while True:
            try:
                # Manual job processing without RQ's polling
                from rq import Queue
                queue = Queue('default', connection=connection)
                
                jobs_processed = 0
                
                # Get all jobs from queue manually
                job_ids = queue.job_ids
                if job_ids:
                    print(f"[{time.strftime('%H:%M:%S')}] Found {len(job_ids)} jobs to process")
                    
                    for job_id in job_ids:
                        try:
                            # Process job using worker
                            worked = worker.work(burst=True, with_scheduler=False)
                            if worked:
                                jobs_processed += 1
                                print(f"[{time.strftime('%H:%M:%S')}] Job {jobs_processed} completed")
                        except Exception as job_error:
                            print(f"Job processing error: {job_error}")
                            continue
                
                if jobs_processed > 0:
                    print(f"[{time.strftime('%H:%M:%S')}] Processed {jobs_processed} jobs total")
                    consecutive_empty = 0
                    # Quick check in case more jobs arrived
                    time.sleep(30)
                else:
                    consecutive_empty += 1
                    # True 5-minute sleep - no Redis polling
                    if consecutive_empty <= 2:
                        sleep_time = 120  # 2 minutes for first 2 empty checks
                        print(f"[{time.strftime('%H:%M:%S')}] No jobs, checking again in {sleep_time//60}min...")
                    else:
                        sleep_time = 300  # 5 minutes after that
                        print(f"[{time.strftime('%H:%M:%S')}] No jobs, sleeping {sleep_time//60}min...")
                    
                    # True sleep - no Redis activity
                    time.sleep(sleep_time)
                    
            except KeyboardInterrupt:
                print("Worker stopped by user")
                break
            except Exception as e:
                if "max requests limit exceeded" in str(e):
                    print("❌ Redis limit exceeded. Stopping worker.")
                    break
                print(f"Worker error: {e}")
                time.sleep(600)  # Wait 10 minutes on error
                
    except Exception as e:
        print(f"Failed to connect to Redis: {e}")

if __name__ == "__main__":
    run_ultra_minimal_worker()
