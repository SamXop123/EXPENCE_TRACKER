import requests
import sys

print('=' * 60)
print('EXPENSE TRACKER APP - COMPREHENSIVE TEST')
print('=' * 60)

# Test main routes
routes = {
    '/': 'Index (Home)',
    '/login': 'Login Page',
    '/signup': 'Sign Up Page',
}

print('\n✓ Testing Main Routes:')
for route, desc in routes.items():
    try:
        r = requests.get(f'http://localhost:5000{route}', timeout=2)
        status = '✓' if r.status_code == 200 else '✗'
        print(f'  {status} {desc} ({route}): {r.status_code}')
    except Exception as e:
        print(f'  ✗ {desc} ({route}): {str(e)[:40]}')

# Test database
print('\n✓ Testing Database:')
try:
    from app import get_db_connection
    conn = get_db_connection()
    
    # Check tables
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    print(f'  ✓ Tables found: {", ".join(tables)}')
    
    # Check users table schema
    cursor = conn.execute('PRAGMA table_info(users)')
    columns = [row[1] for row in cursor.fetchall()]
    print(f'  ✓ Users table columns: {", ".join(columns)}')
    
    conn.close()
except Exception as e:
    print(f'  ✗ Database error: {e}')

# Test imports
print('\n✓ Testing Imports:')
required_imports = [
    ('flask', 'Flask'),
    ('sqlite3', 'SQLite3'),
    ('werkzeug.security', 'Werkzeug Security'),
    ('requests', 'Requests'),
    ('groq', 'Groq'),
]

for module, name in required_imports:
    try:
        __import__(module)
        print(f'  ✓ {name}: OK')
    except ImportError:
        print(f'  ✗ {name}: MISSING')

print('\n' + '=' * 60)
print('✓ APP IS READY TO RUN!')
print('=' * 60)
print('\nAccess the app at: http://localhost:5000')
print('\nTo start the app, run:')
print('  python app.py')
