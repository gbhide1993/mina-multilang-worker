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
    
    prompt = f"""Extract actionable tasks from this voice note transcript.
Return ONLY a JSON array of tasks. Each task should have:
- title: Brief task description
- deadline: ISO date if mentioned, else null
- project: Project/client name if mentioned, else null

Transcript:
{transcript}

Return format: [{{"title": "...", "deadline": "2024-01-15", "project": "..."}}]
If no tasks found, return: []"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        
        tasks_json = _parse_json_response(response.choices[0].message.content)
        
        # Create tasks in database
        created_tasks = []
        for task_data in tasks_json:
            task_id = create_task(
                phone=phone,
                title=task_data.get('title', ''),
                deadline=task_data.get('deadline'),
                project=task_data.get('project'),
                source='voice_note'
            )
            if task_id:
                created_tasks.append(task_data)
        
        return created_tasks
        
    except Exception as e:
        print(f"Task extraction error: {e}")
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
