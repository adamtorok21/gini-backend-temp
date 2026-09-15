from app.services.openai_client import client
from app.utils.chat_memory import get_conversation_history, trim_history, save_message


async def currency_ai(user_id: str, query: str, language: str = "EN") -> dict:
    try:
        chat_history = get_conversation_history(user_id)
        conversation = trim_history(chat_history)
        
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": f""" 
                    You are an intelligent currency conversion assistant designed for tourists visiting Bali, Indonesia. Your task is to help users convert their home currency into Indonesian Rupiah (IDR) using the most recent exchange rates.
                    STRICT INSTRUCTIONS: 
                    1. RESPONSE LANGUAGE: You MUST respond in the following language: {language}.
                    2. The Response should always be in whatsapp Markdown format.
                    Use the previous context to respond: {conversation}.
                    When a user provides a currency code or name (e.g., 'USD', 'Euro', 'Japanese Yen') and an amount, respond ONLY with:
                    1- The converted value in IDR.
                    2- A brief sentence to help tourists understand the value in practical terms (e.g., 'This is enough for a mid-range dinner for two in Bali.')
                    Be friendly and helpful, like a local guide.
                """},
                {"role": "user", "content": query}
            ]
        )
        
        output_text = response.choices[0].message.content
        save_message(user_id, "user", query)
        save_message(user_id, "assistant", output_text)
        
        return output_text
    
    except Exception as e:
        print(f"Error generating currency conversion: {e}")
        raise e