






# def verify_webhook_signature(payload: bytes, signature: str) -> bool:
#     """Verify webhook signature for security"""
#     try:
#         expected_signature = hmac.new(
#             APP_SECRET.encode(),
#             payload,
#             hashlib.sha256
#         ).hexdigest()
        
#         # Remove 'sha256=' prefix if present
#         if signature.startswith("sha256="):
#             signature = signature[7:]
            
#         return hmac.compare_digest(expected_signature, signature)
#     except Exception as e:
#         print(f"Signature verification error: {str(e)}")
#         return False

# def decrypt_flow_data(encrypted_data: str) -> dict:
#     """Decrypt flow data using private key"""
#     try:
#         # Load your private key
#         private_key = serialization.load_pem_private_key(
#             PRIVATE_KEY_PEM.encode(),
#             password=None,
#             backend=default_backend()
#         )
        
#         # Decode base64 encrypted data
#         encrypted_bytes = base64.b64decode(encrypted_data)
        
#         # Decrypt the data
#         decrypted_bytes = private_key.decrypt(
#             encrypted_bytes,
#             padding.OAEP(
#                 mgf=padding.MGF1(algorithm=hashes.SHA256()),
#                 algorithm=hashes.SHA256(),
#                 label=None
#             )
#         )
        
#         # Parse JSON
#         return json.loads(decrypted_bytes.decode())
        
#     except Exception as e:
#         print(f"Decryption error: {str(e)}")
#         return {}

# def extract_sender_from_flow_token(flow_token: str) -> str:
#     """Extract sender_id from flow_token"""
#     try:
#         if flow_token.startswith("date_picker_"):
#             parts = flow_token.split("_")
#             return parts[2] if len(parts) > 2 else None
#         return None
#     except:
#         return None

# async def process_flow_response(sender_id: str, response_data: dict, flow_token: str):
#     """Process the flow response based on flow type"""
#     try:
#         if flow_token.startswith("date_picker_"):
#             await handle_date_selection(sender_id, response_data)
#         else:
#             print(f"Unknown flow type: {flow_token}")
            
#     except Exception as e:
#         print(f"Error processing flow response: {str(e)}")

# async def handle_date_selection(sender_id: str, response_data: dict):
#     """Handle the date selection from the flow"""
#     try:
#         # Extract selected date from response
#         selected_date = response_data.get("selected_date") or response_data.get("calendar")
        
#         if not selected_date:
#             await send_whatsapp_message(sender_id, "No date selected. Please try again.")
#             return
        
#         # Parse the selected date
#         try:
#             user_date = datetime.datetime.strptime(selected_date, '%Y-%m-%d')
#         except ValueError:
#             # Try alternative date format
#             try:
#                 user_date = datetime.datetime.strptime(selected_date, '%d-%m-%Y')
#             except ValueError:
#                 await send_whatsapp_message(sender_id, "Invalid date format received. Please try again.")
#                 return
        
#         # Validate the date
#         today = datetime.datetime.today()
#         if user_date.date() < today.date():
#             await send_whatsapp_message(
#                 sender_id, 
#                 "The date you selected is in the past. Please select today's date or a future date."
#             )
#             await send_date_picker_flow(sender_id)
#             return
        
#         # Check if user has an active session
#         # You'll need to import active_chat_sessions from your main code
#         if sender_id not in active_chat_sessions:
#             await send_whatsapp_message(sender_id, "Please select a service first.")
#             return
        
#         # Store the selected date
#         active_chat_sessions[sender_id].date = user_date
        
#         # Send time slot selection
#         time_slots = [
#             {"title": "08:00 AM - 10:00 AM"},
#             {"title": "10:00 AM - 12:00 AM"},
#             {"title": "12:00 PM - 2:00 PM"},
#             {"title": "02:00 PM - 4:00 PM"},
#             {"title": "04:00 PM - 6:00 PM"},
#             {"title": "06:00 PM - 8:00 PM"}
#         ]
        
#         # Format the date for display
#         formatted_date = user_date.strftime('%d-%m-%Y')
        
#         # Send note about time window
#         await send_whatsapp_message(
#             sender_id, 
#             f"*Note*: _To avoid scheduling issues, please select a 2-hour time window for your service. While our providers aim to start at your chosen time, this window helps prevent delays and ensures a smooth experience. Thanks for your understanding!_"
#         )
        
#         # Send time slots
#         await send_slots_message(sender_id, {
#             "header": f"Time Slots for {formatted_date}",
#             "body": "Please choose your preferred time slot:",
#             "footer": "Available slots",
#             "sections": [
#                 {
#                     "title": "Time Slots",
#                     "rows": time_slots
#                 }
#             ]
#         })
        
#     except Exception as e:
#         print(f"Error handling date selection: {str(e)}")
#         await send_whatsapp_message(sender_id, "Sorry, there was an error processing your date selection. Please try again.")

# async def send_date_picker_flow(sender_id: str):
#     """Send the date picker WhatsApp Flow to the user with dynamic min-date"""
    
#     today = datetime.datetime.now().strftime('%Y-%m-%d')
#     max_date = (datetime.datetime.now() + datetime.timedelta(days=90)).strftime('%Y-%m-%d')
    
#     flow_message = {
#         "messaging_product": "whatsapp",
#         "to": sender_id,
#         "type": "interactive",
#         "interactive": {
#             "type": "flow",
#             "header": {
#                 "type": "text",
#                 "text": "Select Date"
#             },
#             "body": {
#                 "text": "Please choose your preferred appointment date:"
#             },
#             "footer": {
#                 "text": "Choose a date to continue"
#             },
#             "action": {
#                 "name": "flow",
#                 "parameters": {
#                     "flow_message_version": "3",
#                     "flow_token": f"date_picker_{sender_id}_{int(time.time())}",
#                     "flow_id": "YOUR_FLOW_ID_HERE",  # Replace with your actual flow ID
#                     "flow_cta": "Select Date",
#                     "flow_action": "navigate",
#                     "flow_action_payload": {
#                         "screen": "date_picker",
#                         "data": {
#                             "min_date": today,
#                             "max_date": max_date
#                         }
#                     }
#                 }
#             }
#         }
#     }
    
#     return await send_whatsapp_request(flow_message)