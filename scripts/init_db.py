#!/usr/bin/env python3
"""Initialize the Store Intelligence database tables."""
import sys
import os

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import init_db, engine

if __name__ == "__main__":
    try:
        init_db()
        print(f"✓ Database initialized at: {engine.url}")
    except Exception as exc:
        print(f"✗ Failed to initialize database: {exc}", file=sys.stderr)
        sys.exit(1)
