import base64
import json
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding, serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend

class FlowCrypto:
    def __init__(self, private_key_path=None, private_key_content=None, password=None):
        """Initialize with your encrypted private key"""
        
        # Convert password to bytes if provided as string
        if password and isinstance(password, str):
            password = password.encode('utf-8')
        
        try:
            if private_key_path:
                with open(private_key_path, 'rb') as key_file:
                    self.private_key = serialization.load_pem_private_key(
                        key_file.read(),
                        password=password,
                        backend=default_backend()
                    )
            elif private_key_content:
                if isinstance(private_key_content, str):
                    private_key_content = private_key_content.encode()
                self.private_key = serialization.load_pem_private_key(
                    private_key_content,
                    password=password,
                    backend=default_backend()
                )
            else:
                raise ValueError("Either private_key_path or private_key_content must be provided")
                
            print("✅ Private key loaded successfully!")
            
        except ValueError as e:
            if "Bad decrypt" in str(e) or "incorrect password" in str(e).lower():
                raise ValueError("Incorrect password for encrypted private key")
            else:
                raise ValueError(f"Error loading private key: {e}")
        except Exception as e:
            raise Exception(f"Unexpected error loading private key: {e}")
    
    def decrypt_request(self, encrypted_flow_data, encrypted_aes_key, initial_vector):
        """Decrypt and return (decrypted_dict, aes_key, iv).
        Callers must pass aes_key and iv to encrypt_response — never stored as instance state
        to avoid race conditions when concurrent requests share this singleton."""
        print("🔓 Starting decryption process...")
        flow_data = base64.b64decode(encrypted_flow_data)
        encrypted_key = base64.b64decode(encrypted_aes_key)
        iv = base64.b64decode(initial_vector)

        # RSA decryption with OAEP + SHA256
        aes_key = self.private_key.decrypt(
            encrypted_key,
            asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )

        # AES-GCM decryption
        body = flow_data[:-16]
        tag = flow_data[-16:]
        decryptor = Cipher(
            algorithms.AES(aes_key),
            modes.GCM(iv, tag),
            backend=default_backend()
        ).decryptor()

        decrypted_data = decryptor.update(body) + decryptor.finalize()
        return json.loads(decrypted_data.decode('utf-8')), aes_key, iv

    def encrypt_response(self, response_data, aes_key, iv):
        """Encrypt response using the aes_key and iv from the matching decrypt_request call."""
        # Flip IV
        flipped_iv = bytes([b ^ 0xFF for b in iv])

        encryptor = Cipher(
            algorithms.AES(aes_key),
            modes.GCM(flipped_iv),
            backend=default_backend()
        ).encryptor()

        ciphertext = encryptor.update(json.dumps(response_data).encode('utf-8')) + encryptor.finalize()
        return base64.b64encode(ciphertext + encryptor.tag).decode('utf-8')