"""Safety-first role dispatcher for TWO independent Railway services."""
import logging
import os

def main():
    role=os.getenv("MARKETWIDE_ROLE","").strip().lower()
    if role=="screener":
        from marketwide_screener import main as run
    elif role=="buyer":
        from marketwide_buyer import main as run
    else:
        raise RuntimeError("MARKETWIDE_ROLE_REQUIRED: screener or buyer; no trade attempted")
    run()

if __name__=="__main__":
    main()
