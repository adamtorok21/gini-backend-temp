from typing import List

chat_memory = {}
MAX_HISTORY_LENGTH = 10


def save_message(user_id: str, role: str, content: str):
    """
    Save a message in memory for a specific user.
    """
    if user_id not in chat_memory:
        chat_memory[user_id] = []
    chat_memory[user_id].append({"role": role, "content": content})

    if len(chat_memory[user_id]) > MAX_HISTORY_LENGTH:
        chat_memory[user_id] = chat_memory[user_id][-MAX_HISTORY_LENGTH:]


def get_conversation_history(user_id: str) -> List[dict]:
    """
    Retrieve the conversation history for a user.
    """
    return chat_memory.get(user_id, [])


def trim_history(messages: List[dict]) -> List[dict]:
    """
    Trim the conversation history to avoid exceeding token limits.
    """
    return messages[-MAX_HISTORY_LENGTH:]
