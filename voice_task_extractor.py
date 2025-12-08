"""
Voice Task Extractor - Extract actionable tasks from voice note transcripts
"""

import json
import os
from openai import OpenAI
from db import get_conn, create_task

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def extract_tasks_from_transcript(transcript, phone):
    """Extract tasks from transcript using LLM"""
    
    from datetime import datetime, timedelta
    import pytz
    
    # Get current date in IST
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist)
    today_str = today.strftime('%Y-%m-%d')
    tomorrow_str = (today + timedelta(days=1)).strftime('%Y-%m-%d')
    day_name = today.strftime('%A')
    
    prompt = f"""You are an AI assistant that extracts actionable tasks from voice notes.

TODAY'S DATE: {today_str} ({day_name})
TOMORROW: {tomorrow_str}

Analyze this transcript and identify ALL tasks, to-dos, reminders, and action items.
Look for:
- Direct tasks: "I need to...", "I have to...", "I should...", "remind me to..."
- Commitments: "I'll...", "I will...", "I'm going to..."
- Deadlines: "by tomorrow", "next week", "on Monday", specific dates/times
- Hindi/Marathi words: "उद्या" (tomorrow), "आज" (today), "परवा" (day after tomorrow)
- Meetings: "meeting with...", "call with...", "discuss with..."
- Follow-ups: "follow up on...", "check on...", "get back to..."
- Purchases/errands: "buy...", "get...", "pick up..."
- Work items: "finish...", "complete...", "submit...", "send..."

Transcript:
{transcript}

Return ONLY valid JSON array. Each task must have:
- title: Clear, actionable task description (required)
- deadline: ISO date YYYY-MM-DD if mentioned (calculate from TODAY'S DATE above), else null
- project: Project/client/category name if mentioned, else null

Examples:
- "I need to call John tomorrow" → {{"title": "Call John", "deadline": "{tomorrow_str}", "project": null}}
- "Buy groceries" → {{"title": "Buy groceries", "deadline": null, "project": null}}
- "Finish the report for ABC client by Friday" → {{"title": "Finish report", "deadline": "YYYY-MM-DD", "project": "ABC"}}

Return format: [{{"title": "...", "deadline": "...", "project": "..."}}]
If NO tasks found, return: []"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a task extraction expert. Extract ALL actionable items from voice notes. Be generous - if something sounds like a task, include it. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2
        )
        
        content = response.choices[0].message.content
        print(f"TASK EXTRACTOR: LLM response: {content[:200]}...")
        
        tasks_json = _parse_json_response(content)
        print(f"TASK EXTRACTOR: Parsed {len(tasks_json)} tasks from JSON")
        
        if not tasks_json:
            print(f"TASK EXTRACTOR: No tasks found in transcript")
            return []
        
        # Create tasks in database
        created_tasks = []
        for i, task_data in enumerate(tasks_json):
            title = task_data.get('title', '').strip()
            if not title:
                print(f"TASK EXTRACTOR: Skipping task {i+1} - no title")
                continue
            
            deadline = task_data.get('deadline')
            print(f"TASK EXTRACTOR: Creating task {i+1}: {title} (deadline: {deadline})")
            task = create_task(
                phone_or_user_id=phone,
                title=title,
                due_at=deadline,
                source='voice_note',
                metadata={'project': task_data.get('project'), 'transcript_snippet': transcript[:100]} if task_data.get('project') else {'transcript_snippet': transcript[:100]}
            )
            if task:
                created_tasks.append(task_data)
                print(f"TASK EXTRACTOR: Task created successfully")
            else:
                print(f"TASK EXTRACTOR: Failed to create task in database")
        
        print(f"TASK EXTRACTOR: Successfully created {len(created_tasks)} tasks")
        return created_tasks
        
    except Exception as e:
        print(f"TASK EXTRACTOR ERROR: {e}")
        import traceback
        traceback.print_exc()
        return []

def _parse_json_response(content):
    """Parse JSON from LLM response"""
    content = content.strip()
    if content.startswith('```json'):
        content = content[7:]
    if content.startswith('```'):
        content = content[3:]
    if content.endswith('```'):
        content = content[:-3]
    return json.loads(content.strip())
