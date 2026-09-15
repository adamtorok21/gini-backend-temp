import re

def clean_price_string(raw_price: str) -> str:
    """
    Extremely robust price cleaner.
    """
    if not raw_price:
        return "0"
    
    # Remove hidden characters, non-breaking spaces, and whitespace
    val = str(raw_price).encode('ascii', 'ignore').decode('ascii').strip()
    
    # Check for common Google Sheet error indicators
    err_keywords = ["#", "REF", "N/A", "VALUE", "NAME", "ERROR", "nan"]
    if any(k in val.upper() for k in err_keywords):
        return "0"
        
    # Extract only digits
    digits = re.sub(r'[^\d]', '', val)
    if digits:
        try:
            return f"{int(digits):,}".replace(',', '.')
        except:
            pass
            
    # If no digits, but not an error, it might be "Free"
    return val if val else "0"
