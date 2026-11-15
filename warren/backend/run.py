"""Run the API:  python run.py   (default http://localhost:5000)"""
import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    # use_reloader=False: the in-memory SQLite database and the worker threads must live in ONE process
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "3000")), debug=False, threaded=True)
