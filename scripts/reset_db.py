"""Reset database — clear all data, keep table structure.

Works with both SQLite (local dev) and PostgreSQL (DigitalOcean).
Does NOT import server.py to avoid triggering server initialization side effects.

Usage: python scripts/reset_db.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from models import db
from flask import Flask

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = Config.SQLALCHEMY_DATABASE_URI
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

with app.app_context():
    for table in reversed(db.metadata.sorted_tables):
        db.session.execute(table.delete())
    db.session.commit()
    print(f"All data cleared from: {Config.SQLALCHEMY_DATABASE_URI}")
