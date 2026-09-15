import hashlib
import json
from app.utils.chat_memory import get_conversation_history, trim_history, save_message
from app.services.openai_client import client
from fastapi import HTTPException


class ResponseOptimizer:
    def __init__(self):
        self.cache = {}
        self.request_count = 0

    def _gen_cache_key(self, user_id: str, messages: list) -> str:
        key_data = f"{user_id}-{json.dumps(messages, sort_keys=True)}"
        return hashlib.sha256(key_data.encode()).hexdigest()

    async def quantum_response(self, user_id: str, prompt: str) -> str:
        # Get conversation history
        history = get_conversation_history(user_id)
        full_context = trim_history(history + [{"role": "user", "content": prompt}])
        
        cache_key = self._gen_cache_key(user_id, full_context)
        self.request_count += 1
        
        if cache_key in self.cache:
            return self.cache[cache_key]
        
        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "system",
                    "content": f"""🔥ULTRA TUTOR PROFILE🔥
                    Context from previous interactions: {history}
                    You are the world's best Indonesian/Balinese tutor. Rules:
                    1. Maintain conversation continuity with user {user_id}
                    2. Reference previous interactions when relevant
                    3. Layer responses with cultural and linguistic depth"""
                }, {
                    "role": "user",
                    "content": prompt
                }],
                temperature=0.3,
                max_tokens=300,
                frequency_penalty=0.5
            )
            
            result = response.choices[0].message.content
            self.cache[cache_key] = result
            
            # Update memory
            save_message(user_id, "user", prompt)
            save_message(user_id, "assistant", result)
            
            return result

        except Exception as e:
            print(f"Error in quantum_response: {e}")
            raise HTTPException(status_code=500, detail=str(e))

optimizer = ResponseOptimizer()