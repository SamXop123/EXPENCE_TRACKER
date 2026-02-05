import pytest
import sqlite3
import os
from app import app, get_db_connection, init_db

# --- TEST 1: Check Imports ---
def test_imports():
    """Ensures all required modules are installed."""
    required_imports = [
        'flask', 'sqlite3', 'werkzeug.security', 'requests', 'groq', 'xhtml2pdf'
    ]
    for module in required_imports:
        try:
            __import__(module)
        except ImportError:
            pytest.fail(f"Missing required module: {module}")

# --- TEST 2: Check Database Schema ---
def test_database_schema():
    """Checks if the database initializes with correct tables."""
    # Use a temporary file-based DB or memory DB for testing context
    with app.app_context():
        # Ensure we can init without crashing
        try:
            init_db()
        except Exception as e:
            pytest.fail(f"Database initialization failed: {e}")

        conn = get_db_connection()
        
        # Check tables exist
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        expected_tables = ['users', 'expenses', 'budgets', 'categories', 'groups']
        
        for table in expected_tables:
            assert table in tables, f"Database missing table: {table}"
            
        conn.close()

# --- TEST 3: Check Routes ---
def test_routes():
    """Checks if critical pages load (200 OK or 302 Redirect)."""
    with app.test_client() as client:
        routes = {
            '/': [200, 302],
            '/login': [200],
            '/signup': [200],
        }

        for route, valid_codes in routes.items():
            response = client.get(route)
            assert response.status_code in valid_codes, \
                f"Route {route} failed. Got {response.status_code}"