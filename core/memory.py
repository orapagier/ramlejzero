import sqlite3
import json
import os
from typing import List, Dict, Any
from core.config_loader import get_settings

DB_PATH = "logs/memory.db"

def _get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_memory (
            user_id INTEGER PRIMARY KEY,
            history TEXT
        )
    ''')
    conn.commit()
    return conn

def get_history(user_id: int) -> List[Dict[str, Any]]:
    """Retrieve the conversation history for a specific user ID."""
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute('SELECT history FROM user_memory WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return []
    return []

def save_history(user_id: int, history: List[Dict[str, Any]]):
    """Save the conversation history, enforcing the memory_window size."""
    settings = get_settings()
    # Default to 10 messages (5 past turns of user + assistant)
    memory_window = settings.get("agent", {}).get("memory_window", 10)
    
    limited_history = history[-memory_window:] if memory_window > 0 else []
    
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_memory (user_id, history)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET history = excluded.history
    ''', (user_id, json.dumps(limited_history)))
    conn.commit()
    conn.close()
    return limited_history

def clear_history(user_id: int):
    """Clear the memory for a specific user ID."""
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM user_memory WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()
