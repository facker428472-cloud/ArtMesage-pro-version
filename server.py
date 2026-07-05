import os
import datetime
import hashlib
import json
import sqlite3
import time
import threading
import base64
import uuid
from flask import Flask, request, jsonify, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS
from functools import wraps
from io import BytesIO
import mimetypes

# ==================== КОНФИГУРАЦИЯ ====================
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'artmessage-secret-key-2026')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024
CORS(app, origins='*')
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='eventlet')

DB_PATH = os.environ.get('DB_PATH', 'artmessage.db')
PHOTOS_DIR = os.environ.get('PHOTOS_DIR', 'photos')

os.makedirs(PHOTOS_DIR, exist_ok=True)

# ==================== БАЗА ДАННЫХ ====================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            first_name TEXT,
            last_name TEXT,
            is_online BOOLEAN DEFAULT 0,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT DEFAULT 'private',
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_members (
            chat_id INTEGER,
            user_id INTEGER,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_pinned BOOLEAN DEFAULT 0,
            mute_until TIMESTAMP,
            PRIMARY KEY (chat_id, user_id),
            FOREIGN KEY (chat_id) REFERENCES chats(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            text TEXT,
            photo_id TEXT,
            reply_to_id INTEGER,
            is_edited BOOLEAN DEFAULT 0,
            edited_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chats(id),
            FOREIGN KEY (sender_id) REFERENCES users(id),
            FOREIGN KEY (reply_to_id) REFERENCES messages(id)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS message_status (
            message_id INTEGER,
            user_id INTEGER,
            status TEXT DEFAULT 'sent',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (message_id, user_id),
            FOREIGN KEY (message_id) REFERENCES messages(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    
    conn.commit()
    conn.close()
    print("Database initialized")

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, password_hash):
    return hash_password(password) == password_hash

def get_user_by_id(user_id):
    conn = get_db()
    cursor = conn.cursor()
    user = cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return user

def get_user_by_username(username):
    conn = get_db()
    cursor = conn.cursor()
    user = cursor.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    return user

def create_chat(chat_type='private', name=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO chats (type, name) VALUES (?, ?)', (chat_type, name))
    chat_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return chat_id

def add_member_to_chat(chat_id, user_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)', (chat_id, user_id))
        conn.commit()
        conn.close()
        return True
    except:
        conn.close()
        return False

def get_chat_between_users(user1_id, user2_id):
    conn = get_db()
    cursor = conn.cursor()
    chat = cursor.execute('''
        SELECT c.id FROM chats c
        JOIN chat_members cm1 ON c.id = cm1.chat_id AND cm1.user_id = ?
        JOIN chat_members cm2 ON c.id = cm2.chat_id AND cm2.user_id = ?
        WHERE c.type = 'private'
        GROUP BY c.id
        HAVING COUNT(DISTINCT cm1.user_id) = 2 AND COUNT(DISTINCT cm2.user_id) = 2
    ''', (user1_id, user2_id)).fetchone()
    conn.close()
    return chat

def get_all_chats(user_id):
    conn = get_db()
    cursor = conn.cursor()
    chats = cursor.execute('''
        SELECT 
            c.id,
            c.type,
            c.name,
            cm.is_pinned,
            (
                SELECT text FROM messages 
                WHERE chat_id = c.id 
                ORDER BY created_at DESC LIMIT 1
            ) as last_message,
            (
                SELECT photo_id FROM messages 
                WHERE chat_id = c.id 
                ORDER BY created_at DESC LIMIT 1
            ) as last_photo_id,
            (
                SELECT created_at FROM messages 
                WHERE chat_id = c.id 
                ORDER BY created_at DESC LIMIT 1
            ) as last_message_time,
            (
                SELECT COUNT(*) FROM message_status ms
                JOIN messages m ON ms.message_id = m.id
                WHERE m.chat_id = c.id 
                AND ms.user_id = ? 
                AND ms.status = 'sent'
            ) as unread_count
        FROM chats c
        JOIN chat_members cm ON c.id = cm.chat_id
        WHERE cm.user_id = ?
        ORDER BY cm.is_pinned DESC, last_message_time DESC NULLS LAST
    ''', (user_id, user_id)).fetchall()
    conn.close()
    return chats

def get_chat_members(chat_id):
    conn = get_db()
    cursor = conn.cursor()
    members = cursor.execute('''
        SELECT u.id, u.username, u.first_name, u.last_name, u.is_online
        FROM chat_members cm
        JOIN users u ON cm.user_id = u.id
        WHERE cm.chat_id = ?
    ''', (chat_id,)).fetchall()
    conn.close()
    return members

def get_messages(chat_id, user_id, limit=50, last_id=None):
    conn = get_db()
    cursor = conn.cursor()
    
    if last_id:
        messages = cursor.execute('''
            SELECT m.*, u.username, u.first_name, u.last_name,
                   ms.status as my_status
            FROM messages m
            JOIN users u ON m.sender_id = u.id
            LEFT JOIN message_status ms ON m.id = ms.message_id AND ms.user_id = ?
            WHERE m.chat_id = ? AND m.id < ?
            ORDER BY m.created_at DESC
            LIMIT ?
        ''', (user_id, chat_id, last_id, limit)).fetchall()
    else:
        messages = cursor.execute('''
            SELECT m.*, u.username, u.first_name, u.last_name,
                   ms.status as my_status
            FROM messages m
            JOIN users u ON m.sender_id = u.id
            LEFT JOIN message_status ms ON m.id = ms.message_id AND ms.user_id = ?
            WHERE m.chat_id = ?
            ORDER BY m.created_at DESC
            LIMIT ?
        ''', (user_id, chat_id, limit)).fetchall()
    
    conn.close()
    return list(reversed(messages))

def save_message(chat_id, sender_id, text=None, photo_id=None, reply_to_id=None):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO messages (chat_id, sender_id, text, photo_id, reply_to_id)
        VALUES (?, ?, ?, ?, ?)
    ''', (chat_id, sender_id, text, photo_id, reply_to_id))
    message_id = cursor.lastrowid
    
    members = get_chat_members(chat_id)
    for member in members:
        status = 'read' if member['id'] == sender_id else 'sent'
        cursor.execute('''
            INSERT INTO message_status (message_id, user_id, status)
            VALUES (?, ?, ?)
        ''', (message_id, member['id'], status))
    
    conn.commit()
    conn.close()
    return message_id

def mark_message_as_read(message_id, user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE message_status 
        SET status = 'read', updated_at = CURRENT_TIMESTAMP
        WHERE message_id = ? AND user_id = ?
    ''', (message_id, user_id))
    conn.commit()
    conn.close()

def update_user_online(user_id, is_online=True):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users 
        SET is_online = ?, last_seen = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (1 if is_online else 0, user_id))
    conn.commit()
    conn.close()

def search_users(query, current_user_id):
    conn = get_db()
    cursor = conn.cursor()
    users = cursor.execute('''
        SELECT id, username, first_name, last_name, is_online
        FROM users
        WHERE (username LIKE ? OR first_name LIKE ? OR last_name LIKE ?)
        AND id != ?
        LIMIT 20
    ''', (f'%{query}%', f'%{query}%', f'%{query}%', current_user_id)).fetchall()
    conn.close()
    return users

def save_photo(photo_data_base64):
    try:
        photo_id = str(uuid.uuid4())
        filename = f"{photo_id}.jpg"
        filepath = os.path.join(PHOTOS_DIR, filename)
        
        photo_bytes = base64.b64decode(photo_data_base64)
        with open(filepath, 'wb') as f:
            f.write(photo_bytes)
        
        return photo_id
    except Exception as e:
        print(f"Photo save error: {e}")
        return None

def get_photo_path(photo_id):
    if not photo_id:
        return None
    
    extensions = ['jpg', 'jpeg', 'png', 'gif', 'webp']
    for ext in extensions:
        filepath = os.path.join(PHOTOS_DIR, f"{photo_id}.{ext}")
        if os.path.exists(filepath):
            return filepath
    
    filepath = os.path.join(PHOTOS_DIR, photo_id)
    if os.path.exists(filepath):
        return filepath
    
    return None

def get_message_chat_id(message_id):
    conn = get_db()
    cursor = conn.cursor()
    result = cursor.execute('SELECT chat_id FROM messages WHERE id = ?', (message_id,)).fetchone()
    conn.close()
    return result['chat_id'] if result else None

# ==================== ДЕКОРАТОРЫ ====================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_id = request.args.get('user_id') or request.json.get('user_id') if request.json else None
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401
        
        user = get_user_by_id(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 401
        
        return f(*args, **kwargs)
    return decorated_function

# ==================== API - АВТОРИЗАЦИЯ ====================
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    
    username = data.get('username')
    password = data.get('password')
    first_name = data.get('first_name', '')
    last_name = data.get('last_name', '')
    
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    
    existing = get_user_by_username(username)
    if existing:
        return jsonify({'error': 'User already exists'}), 400
    
    password_hash = hash_password(password)
    
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO users (username, password_hash, first_name, last_name)
            VALUES (?, ?, ?, ?)
        ''', (username, password_hash, first_name, last_name))
        user_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'user_id': user_id,
            'username': username,
            'first_name': first_name,
            'last_name': last_name
        }), 201
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    
    user = get_user_by_username(username)
    if not user:
        return jsonify({'error': 'User not found'}), 401
    
    if not verify_password(password, user['password_hash']):
        return jsonify({'error': 'Invalid password'}), 401
    
    update_user_online(user['id'], True)
    
    return jsonify({
        'success': True,
        'user_id': user['id'],
        'username': user['username'],
        'first_name': user['first_name'],
        'last_name': user['last_name'],
        'is_online': True
    }), 200

@app.route('/api/logout', methods=['POST'])
def logout():
    data = request.json
    user_id = data.get('user_id')
    
    if user_id:
        update_user_online(user_id, False)
    
    return jsonify({'success': True}), 200

# ==================== API - ЧАТЫ ====================
@app.route('/api/chats', methods=['GET'])
@login_required
def get_user_chats():
    user_id = request.args.get('user_id')
    
    chats = get_all_chats(user_id)
    
    result = []
    for chat in chats:
        chat_dict = dict(chat)
        if chat_dict['last_message_time']:
            chat_dict['last_message_time'] = chat_dict['last_message_time']
        chat_dict['members'] = [dict(m) for m in get_chat_members(chat['id'])]
        result.append(chat_dict)
    
    return jsonify(result), 200

@app.route('/api/chats/create', methods=['POST'])
@login_required
def create_new_chat():
    data = request.json
    user_id = data.get('user_id')
    peer_id = data.get('peer_id')
    
    if not peer_id:
        return jsonify({'error': 'Peer not specified'}), 400
    
    existing = get_chat_between_users(user_id, peer_id)
    if existing:
        return jsonify({'chat_id': existing['id'], 'exists': True}), 200
    
    chat_id = create_chat('private')
    add_member_to_chat(chat_id, user_id)
    add_member_to_chat(chat_id, peer_id)
    
    return jsonify({'chat_id': chat_id, 'exists': False}), 201

# ==================== API - СООБЩЕНИЯ ====================
@app.route('/api/messages', methods=['GET'])
@login_required
def get_chat_messages():
    chat_id = request.args.get('chat_id')
    user_id = request.args.get('user_id')
    limit = request.args.get('limit', 50, type=int)
    last_id = request.args.get('last_id', type=int)
    
    if not chat_id:
        return jsonify({'error': 'Chat ID required'}), 400
    
    messages = get_messages(chat_id, user_id, limit, last_id)
    
    result = []
    for msg in messages:
        msg_dict = dict(msg)
        msg_dict['created_at'] = msg_dict['created_at']
        if msg_dict['photo_id']:
            msg_dict['photo_url'] = f"/api/photos/{msg_dict['photo_id']}"
        result.append(msg_dict)
    
    return jsonify(result), 200

@app.route('/api/messages/<int:message_id>/read', methods=['POST'])
@login_required
def mark_read():
    data = request.json
    user_id = data.get('user_id')
    message_id = request.view_args['message_id']
    
    mark_message_as_read(message_id, user_id)
    
    socketio.emit('message_read', {
        'message_id': message_id,
        'user_id': user_id
    }, room=f'chat_{get_message_chat_id(message_id)}')
    
    return jsonify({'success': True}), 200

# ==================== API - ФОТО ====================
@app.route('/api/photos/upload', methods=['POST'])
@login_required
def upload_photo():
    data = request.json
    user_id = data.get('user_id')
    chat_id = data.get('chat_id')
    photo_data = data.get('photo')
    text = data.get('text', '')
    
    if not photo_data:
        return jsonify({'error': 'Photo data required'}), 400
    
    if not chat_id:
        return jsonify({'error': 'Chat ID required'}), 400
    
    photo_id = save_photo(photo_data)
    if not photo_id:
        return jsonify({'error': 'Failed to save photo'}), 400
    
    message_id = save_message(chat_id, user_id, text, photo_id)
    
    conn = get_db()
    cursor = conn.cursor()
    message = cursor.execute('''
        SELECT m.*, u.username, u.first_name, u.last_name
        FROM messages m
        JOIN users u ON m.sender_id = u.id
        WHERE m.id = ?
    ''', (message_id,)).fetchone()
    conn.close()
    
    if message:
        msg_dict = dict(message)
        msg_dict['created_at'] = msg_dict['created_at']
        msg_dict['photo_url'] = f"/api/photos/{msg_dict['photo_id']}"
        
        room = f'chat_{chat_id}'
        socketio.emit('new_message', msg_dict, room=room)
        
        return jsonify({
            'success': True,
            'message_id': message_id,
            'photo_id': photo_id,
            'photo_url': f"/api/photos/{photo_id}"
        }), 201
    
    return jsonify({'error': 'Failed to create message'}), 400

@app.route('/api/photos/<photo_id>', methods=['GET'])
def get_photo(photo_id):
    filepath = get_photo_path(photo_id)
    
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'Photo not found'}), 404
    
    mime_type, _ = mimetypes.guess_type(filepath)
    if not mime_type:
        mime_type = 'image/jpeg'
    
    return send_file(filepath, mimetype=mime_type)

# ==================== API - ПОИСК ====================
@app.route('/api/users/search', methods=['GET'])
@login_required
def search():
    query = request.args.get('q', '')
    user_id = request.args.get('user_id')
    
    if len(query) < 2:
        return jsonify([]), 200
    
    users = search_users(query, user_id)
    return jsonify([dict(u) for u in users]), 200

# ==================== WEBSOCKET ====================
@socketio.on('connect')
def handle_connect():
    user_id = request.args.get('user_id')
    if user_id:
        update_user_online(user_id, True)
        print(f'User {user_id} connected')
        
        socketio.emit('user_online', {
            'user_id': int(user_id),
            'is_online': True
        }, broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    user_id = request.args.get('user_id')
    if user_id:
        update_user_online(user_id, False)
        print(f'User {user_id} disconnected')
        
        socketio.emit('user_online', {
            'user_id': int(user_id),
            'is_online': False
        }, broadcast=True)

@socketio.on('join_chat')
def handle_join_chat(data):
    user_id = data.get('user_id')
    chat_id = data.get('chat_id')
    
    if user_id and chat_id:
        room = f'chat_{chat_id}'
        join_room(room)
        print(f'User {user_id} joined chat {chat_id}')
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE message_status 
            SET status = 'read', updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND message_id IN (
                SELECT id FROM messages WHERE chat_id = ?
            )
        ''', (user_id, chat_id))
        conn.commit()
        conn.close()

@socketio.on('leave_chat')
def handle_leave_chat(data):
    user_id = data.get('user_id')
    chat_id = data.get('chat_id')
    
    if user_id and chat_id:
        room = f'chat_{chat_id}'
        leave_room(room)
        print(f'User {user_id} left chat {chat_id}')

@socketio.on('send_message')
def handle_send_message(data):
    user_id = data.get('user_id')
    chat_id = data.get('chat_id')
    text = data.get('text')
    reply_to_id = data.get('reply_to_id')
    
    if not user_id or not chat_id:
        emit('error', {'error': 'Insufficient data'})
        return
    
    if not text:
        emit('error', {'error': 'Message text is empty'})
        return
    
    message_id = save_message(chat_id, user_id, text, None, reply_to_id)
    
    conn = get_db()
    cursor = conn.cursor()
    message = cursor.execute('''
        SELECT m.*, u.username, u.first_name, u.last_name
        FROM messages m
        JOIN users u ON m.sender_id = u.id
        WHERE m.id = ?
    ''', (message_id,)).fetchone()
    conn.close()
    
    if message:
        msg_dict = dict(message)
        msg_dict['created_at'] = msg_dict['created_at']
        
        room = f'chat_{chat_id}'
        emit('new_message', msg_dict, room=room)
        print(f'Message {message_id} sent to chat {chat_id}')

@socketio.on('send_photo')
def handle_send_photo(data):
    user_id = data.get('user_id')
    chat_id = data.get('chat_id')
    photo_data = data.get('photo')
    text = data.get('text', '')
    
    if not user_id or not chat_id or not photo_data:
        emit('error', {'error': 'Insufficient data'})
        return
    
    photo_id = save_photo(photo_data)
    if not photo_id:
        emit('error', {'error': 'Failed to save photo'})
        return
    
    message_id = save_message(chat_id, user_id, text, photo_id)
    
    conn = get_db()
    cursor = conn.cursor()
    message = cursor.execute('''
        SELECT m.*, u.username, u.first_name, u.last_name
        FROM messages m
        JOIN users u ON m.sender_id = u.id
        WHERE m.id = ?
    ''', (message_id,)).fetchone()
    conn.close()
    
    if message:
        msg_dict = dict(message)
        msg_dict['created_at'] = msg_dict['created_at']
        msg_dict['photo_url'] = f"/api/photos/{msg_dict['photo_id']}"
        
        room = f'chat_{chat_id}'
        emit('new_message', msg_dict, room=room)
        print(f'Photo {message_id} sent to chat {chat_id}')

@socketio.on('typing')
def handle_typing(data):
    user_id = data.get('user_id')
    chat_id = data.get('chat_id')
    is_typing = data.get('is_typing', False)
    
    if user_id and chat_id:
        user = get_user_by_id(user_id)
        if user:
            room = f'chat_{chat_id}'
            emit('user_typing', {
                'user_id': user_id,
                'username': user['username'],
                'is_typing': is_typing
            }, room=room, include_self=False)

# ==================== ЗАПУСК ====================
if __name__ == '__main__':
    if not os.path.exists(DB_PATH):
        init_db()
    else:
        print('Database already exists')
    
    print('=' * 50)
    print('ArtMessage Server Started')
    print('WebSocket: ws://localhost:5000')
    print('HTTP API: http://localhost:5000/api')
    print('Photos stored in: ' + PHOTOS_DIR)
    print('=' * 50)
    
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
