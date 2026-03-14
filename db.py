import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'video_generator.db')

def get_connection():
    # Use check_same_thread=False so we can do simple updates from background QThreads
    return sqlite3.connect(DB_PATH, check_same_thread=False) 

def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            duration INTEGER,
            script_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS scenes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER,
            scene_order INTEGER,
            visual_prompt TEXT,
            narration TEXT,
            img_path TEXT,
            audio_path TEXT,
            FOREIGN KEY(topic_id) REFERENCES topics(id)
        )
    ''')
    conn.commit()
    conn.close()

def save_script_and_scenes(topic, duration, script_text, scenes):
    """
    Saves the generated topic and parses scenes into the database.
    Injects 'db_id' into each scene dictionary so we can update asset paths later.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        'INSERT INTO topics (topic, duration, script_text) VALUES (?, ?, ?)',
        (topic, duration, script_text)
    )
    topic_id = c.lastrowid
    
    for i, scene in enumerate(scenes):
        c.execute(
            'INSERT INTO scenes (topic_id, scene_order, visual_prompt, narration) VALUES (?, ?, ?, ?)',
            (topic_id, i+1, scene.get('visual', ''), scene.get('narration', ''))
        )
        # Store the db_id back into the in-memory scene dict so we know which row to update
        scene['db_id'] = c.lastrowid 
        
    conn.commit()
    conn.close()
    return topic_id

def update_scene_asset(scene_id, asset_type, asset_path):
    """
    Updates a specific scene with the generated asset path.
    asset_type must be either 'img_path' or 'audio_path'
    """
    if asset_type not in ['img_path', 'audio_path'] or not scene_id:
        return
        
    conn = get_connection()
    c = conn.cursor()
    c.execute(f'UPDATE scenes SET {asset_type} = ? WHERE id = ?', (asset_path, scene_id))
    conn.commit()
    conn.close()

def get_all_topics():
    """Returns all topics sorted by most recently created first: (id, topic, duration, created_at)"""
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT id, topic, duration, created_at FROM topics ORDER BY created_at DESC')
    rows = c.fetchall()
    conn.close()
    return rows

def get_topic_detail(topic_id):
    """Returns full topic record and all its scenes."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('SELECT id, topic, duration, script_text, created_at FROM topics WHERE id = ?', (topic_id,))
    topic_row = c.fetchone()
    c.execute(
        'SELECT id, scene_order, visual_prompt, narration, img_path, audio_path FROM scenes WHERE topic_id = ? ORDER BY scene_order',
        (topic_id,)
    )
    scenes = c.fetchall()
    conn.close()
    return topic_row, scenes


def delete_topic(topic_id):
    """Delete a topic and all of its scenes."""
    if not topic_id:
        return

    conn = get_connection()
    c = conn.cursor()
    c.execute('DELETE FROM scenes WHERE topic_id = ?', (topic_id,))
    c.execute('DELETE FROM topics WHERE id = ?', (topic_id,))
    conn.commit()
    conn.close()
