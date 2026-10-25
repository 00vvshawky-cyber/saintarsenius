"""Entry point: run the school portal with waitress (production WSGI server).

Usage (from the school-portal directory):
    python run.py

The port is read from the PORT environment variable (default 8000).
"""
import os

from waitress import serve

from backend.app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    print(f"Serving school portal on http://{host}:{port}")
    serve(app, host=host, port=port)
