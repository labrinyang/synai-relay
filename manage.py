"""
Database migration management via Flask-Migrate (Alembic).

Usage:
    flask --app manage:app db init       # Initialize migrations directory
    flask --app manage:app db migrate -m "initial"  # Generate migration
    flask --app manage:app db upgrade    # Apply migrations
    flask --app manage:app db downgrade  # Rollback last migration
"""
from flask import Flask
from flask_migrate import Migrate
from models import db
from config import Config

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# Import all models so Alembic can detect them
from models import Owner, Agent, Job, Submission, IdempotencyKey, Webhook, Dispute, JobParticipant  # noqa: F401

migrate = Migrate(app, db)
