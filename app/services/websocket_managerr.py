from fastapi import WebSocket
import json


class ConnectionManager:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.active_connections = {}
            cls._instance.pending_messages = {}
        return cls._instance
    
    def __init__(self):
        if not hasattr(self, 'active_connections'):
            self.active_connections = {}
            self.pending_messages = {}
    
    async def connect(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[session_id] = websocket

        if session_id in self.pending_messages and self.pending_messages[session_id]:
            for message in self.pending_messages[session_id]:
                await websocket.send_text(message)
            self.pending_messages[session_id] = []
    
    def disconnect(self, session_id: str):
        if session_id in self.active_connections:
            self.active_connections.pop(session_id, None)
            print(f"Disconnected session {session_id}")
            print(f"Remaining active connections: {len(self.active_connections)}")
    
    async def send_personal_message(self, message: str, session_id: str, message_type: str):
        payload = {"type": message_type, "message": message}
        payload_str = json.dumps(payload)


        websocket = self.active_connections.get(session_id)
        if websocket:
            try:
                await websocket.send_text(payload_str)
                print(f"Successfully sent message to {session_id}")
                return True
            except Exception as e:
                print(f"Error sending message to {session_id}: {e}")
                self._queue_message(payload_str, session_id)
                return False
        else:
            print(f"No active connection for session {session_id}, queueing message")
            self._queue_message(payload_str, session_id)
            return False
    
    def _queue_message(self, message: str, session_id: str):
        if session_id not in self.pending_messages:
            self.pending_messages[session_id] = []
        self.pending_messages[session_id].append(message)
    
    async def broadcast(self, message: str):
        disconnected_sessions = []
        for session_id, websocket in self.active_connections.items():
            try:
                await websocket.send_text(message)
            except Exception:
                disconnected_sessions.append(session_id)
        for session_id in disconnected_sessions:
            self.disconnect(session_id)
    
    def get_connection_count(self):
        return len(self.active_connections)
    
    def is_connected(self, session_id: str) -> bool:
        return session_id in self.active_connections
    
    def get_pending_message_count(self, session_id: str = None):
        if session_id:
            return len(self.pending_messages.get(session_id, []))
        else:
            return sum(len(messages) for messages in self.pending_messages.values())