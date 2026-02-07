import pytest
import sqlite3
import os
import uuid
import app as flask_app

@pytest.fixture
def app():
    # Configure app for testing
    flask_app.app.config.update({
        "TESTING": True,
        "JWT_SECRET": "test-secret",
    })
    
    # Use a unique shared in-memory database for this test
    db_name = f"test_{uuid.uuid4().hex}"
    flask_app.DB_PATH = f"file:{db_name}?mode=memory&cache=shared"
    
    # Initialize the database schema
    with flask_app.app.app_context():
        flask_app.init_db()
        
    yield flask_app.app
    
    # No explicit cleanup needed for in-memory DB as it's destroyed when last connection closes
    # But we should ensure all connections are closed if any were leaked.

@pytest.fixture
def client(app):
    return app.test_client()

@pytest.fixture
def runner(app):
    return app.test_cli_runner()

@pytest.fixture
def auth_token(client):
    # Helper to get an auth token
    client.post('/api/auth/signup', json={
        "username": "testuser",
        "email": "test@example.com",
        "password": "password123"
    })
    response = client.post('/api/auth/login', json={
        "username": "testuser",
        "password": "password123"
    })
    return response.get_json()['data']['token']
