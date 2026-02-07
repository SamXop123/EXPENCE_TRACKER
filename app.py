from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
import sqlite3
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import json
import requests
import time
import os
import io
import pandas as pd
import jwt
import pyotp
import qrcode
import io
import base64
from cryptography.fernet import Fernet
from functools import wraps
from flasgger import Swagger, swag_from
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from groq import Groq
from xhtml2pdf import pisa

# --- CONFIGURATION ---
app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # Change to random key

# --- ENCRYPTION CONFIGURATION ---
# Generates a key file if it doesn't exist. 
# IN PRODUCTION: Keep 'secret.key' safe and separate from the code!
KEY_FILE = 'secret.key'

def load_key():
    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        with open(KEY_FILE, 'wb') as key_file:
            key_file.write(key)
    return open(KEY_FILE, 'rb').read()

cipher_suite = Fernet(load_key())

def encrypt_data(data):
    """Encrypts a string."""
    if not data: return ""
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(data):
    """Decrypts a string. Returns original data if decryption fails (Backward Compatibility)."""
    if not data: return ""
    try:
        return cipher_suite.decrypt(data.encode()).decode()
    except Exception:
        return data  # Return raw text if it wasn't encrypted (Legacy Data)

# Swagger Configuration
app.config['SWAGGER'] = {
    'title': 'Expense Tracker API',
    'uiversion': 3,
    'specs_route': '/api/docs/',
    'securityDefinitions': {
        'Bearer': {
            'type': 'apiKey',
            'name': 'Authorization',
            'in': 'header',
            'description': 'JWT Authorization header using the Bearer scheme. Example: \"Authorization: Bearer {token}\"'
        }
    },
    'security': [
        {
            'Bearer': []
        }
    ]
}
swagger = Swagger(app)

# JWT Configuration
app.config['JWT_SECRET'] = 'your-jwt-secret-key' # In production, use environment variable
app.config['JWT_ALGORITHM'] = 'HS256'

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

_RATES_CACHE = {
    "timestamp": 0,
    "rates": {}
}
CACHE_TTL = 60 * 60  # 1 hour

# Initialize Groq Client (Ensure API Key is set)
# Ideally, use os.environ.get("GROQ_API_KEY")
groq_client = Groq(
    api_key="YOUR_GROQ_API_KEY_HERE" 
)

# --- DATABASE HELPERS ---
DB_PATH = 'expenses.db'

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, uri=True)
    conn.row_factory = sqlite3.Row
    return conn

# --- API HELPERS & DECORATORS ---
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            if auth_header.startswith('Bearer '):
                token = auth_header.split(" ")[1]
        
        if not token:
            return jsonify({'message': 'Token is missing!'}), 401
        
        try:
            data = jwt.decode(token, app.config['JWT_SECRET'], algorithms=[app.config['JWT_ALGORITHM']])
            current_user_id = data['user_id']
        except Exception as e:
            return jsonify({'message': 'Token is invalid!', 'error': str(e)}), 401
            
        return f(current_user_id, *args, **kwargs)
    
    return decorated

def api_response(success=True, data=None, message=None, code=200):
    response = {'success': success}
    if data is not None:
        response['data'] = data
    if message is not None:
        response['message'] = message
    return jsonify(response), code

# --- GROUP RBAC HELPERS & DECORATORS ---
def get_user_group_role(user_id, group_id):
    """Get user's role in a specific group"""
    conn = get_db_connection()
    member = conn.execute(
        'SELECT role FROM group_members WHERE group_id = ? AND user_id = ?',
        (group_id, user_id)
    ).fetchone()
    conn.close()
    return member['role'] if member else None

def require_group_role(*allowed_roles):
    """Decorator to check if user has required role in group
    
    Usage: @require_group_role('admin', 'owner')
    
    Roles hierarchy (from lowest to highest):
    viewer < editor < admin < owner
    
    If user has a higher role than required, access is granted.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(group_id, *args, **kwargs):
            # Check user is logged in
            if 'user_id' not in session:
                flash("Please log in to access this page.")
                return redirect(url_for('login'))
            
            # Get user's role in group
            conn = get_db_connection()
            member = conn.execute(
                'SELECT role FROM group_members WHERE group_id = ? AND user_id = ?',
                (group_id, session['user_id'])
            ).fetchone()
            conn.close()
            
            # Check membership
            if not member:
                flash("You are not a member of this group.")
                return redirect(url_for('groups'))
            
            # Define role hierarchy
            role_hierarchy = {'viewer': 1, 'editor': 2, 'admin': 3, 'owner': 4}
            user_role_level = role_hierarchy.get(member['role'], 0)
            required_level = min(role_hierarchy.get(role, 999) for role in allowed_roles)
            
            if user_role_level < required_level:
                flash(f"You need {allowed_roles[0]} permissions for this action.")
                return redirect(url_for('group_detail', group_id=group_id))
            
            return f(group_id, *args, **kwargs)
        return decorated_function
    return decorator

def init_db():
    conn = get_db_connection()
    
    # Users Table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            totp_secret TEXT
        )
    ''')

    # Expenses Table - UPDATED with Recurring Fields
    
    # [MIGRATION] Add totp_secret column if it doesn't exist
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(users)")
    columns = [info[1] for info in cursor.fetchall()]
    if 'totp_secret' not in columns:
        print("Migrating DB: Adding totp_secret to users table...")
        conn.execute('ALTER TABLE users ADD COLUMN totp_secret TEXT')
    
    # Expenses Table (with multi-currency support)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            amount_usd REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            date TEXT NOT NULL,
            is_recurring BOOLEAN DEFAULT 0,
            frequency TEXT DEFAULT 'monthly',
            next_due_date TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # [MIGRATION] Check for new columns in expenses
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(expenses)")
    columns = [info[1] for info in cursor.fetchall()]
    
    if 'is_recurring' not in columns:
        print("Migrating DB: Adding recurring fields...")
        conn.execute('ALTER TABLE expenses ADD COLUMN is_recurring BOOLEAN DEFAULT 0')
        conn.execute('ALTER TABLE expenses ADD COLUMN frequency TEXT DEFAULT "monthly"')
        conn.execute('ALTER TABLE expenses ADD COLUMN next_due_date TEXT')
    
    # Budgets Table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            amount_usd REAL NOT NULL,
            period TEXT NOT NULL DEFAULT 'monthly',
            start_date TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # Categories Table (for dynamic category management)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            icon TEXT DEFAULT '💰',
            color TEXT DEFAULT '#6c757d',
            FOREIGN KEY (user_id) REFERENCES users (id),
            UNIQUE(user_id, name)
        )
    ''')
    
    # Indexes
    conn.execute('CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_expenses_user_category ON expenses(user_id, category)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_budgets_user ON budgets(user_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_categories_user ON categories(user_id)')
    # Analytics-specific indexes
    conn.execute('CREATE INDEX IF NOT EXISTS idx_expenses_user_date_category ON expenses(user_id, date, category)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_expenses_date_amount ON expenses(date, amount_usd)')
    
    # --- NEW SPLITWISE TABLES ---
    conn.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (created_by) REFERENCES users (id)
        )
    ''')
    
    conn.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL,
            FOREIGN KEY (group_id) REFERENCES groups (id),
            FOREIGN KEY (user_id) REFERENCES users (id),
            PRIMARY KEY (group_id, user_id)
        )
    ''')
    
    conn.execute('''
        CREATE TABLE IF NOT EXISTS group_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            payer_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            date TEXT NOT NULL,
            FOREIGN KEY (group_id) REFERENCES groups (id),
            FOREIGN KEY (payer_id) REFERENCES users (id)
        )
    ''')
    
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expense_splits (
            expense_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount_owed REAL NOT NULL,
            FOREIGN KEY (expense_id) REFERENCES group_expenses (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # [MIGRATION] Add role column to group_members if it doesn't exist
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(group_members)")
    columns = [info[1] for info in cursor.fetchall()]
    if 'role' not in columns:
        print("Migrating DB: Adding role column to group_members table...")
        conn.execute('ALTER TABLE group_members ADD COLUMN role TEXT NOT NULL DEFAULT "editor"')
        
        # Update existing members: set role based on group ownership
        # Group creators get 'owner' role
        conn.execute('''
            UPDATE group_members 
            SET role = 'owner' 
            WHERE EXISTS (
                SELECT 1 FROM groups 
                WHERE groups.id = group_members.group_id 
                AND groups.created_by = group_members.user_id
            )
        ''')
        print("Migrated existing group members with appropriate roles.")
    
    conn.commit()
    conn.close()

# --- CURRENCY HELPERS ---
def _fetch_usd_rates():
    try:
        url = "https://api.exchangerate-api.com/v4/latest/USD"
        response = requests.get(url, timeout=5)
        data = response.json()
        if "rates" not in data:
            raise ValueError(f"Invalid API response: {data}")
        return data["rates"]
    except Exception as e:
        print(f"Rate fetch error: {e}")
        return {}

def get_usd_rate(currency):
    if currency == "USD":
        return 1.0
    
    now = time.time()
    if not _RATES_CACHE["rates"] or now - _RATES_CACHE["timestamp"] > CACHE_TTL:
        _RATES_CACHE["rates"] = _fetch_usd_rates()
        _RATES_CACHE["timestamp"] = now
    
    rates = _RATES_CACHE["rates"]
    return float(rates.get(currency, 1.0))

def convert_to_usd(amount, currency):
    rate = get_usd_rate(currency)
    return round(amount / rate, 2)

def convert_from_usd(amount_usd, currency):
    rate = get_usd_rate(currency)
    return round(amount_usd * rate, 2)

# --- NEW HELPER: SPLITWISE DEBT ALGORITHM ---
def calculate_group_debts(group_id):
    """Calculates who owes whom, handling settlements/partial payments."""
    conn = get_db_connection()
    
    # 1. Get Members
    members = conn.execute(
        "SELECT user_id, username FROM group_members JOIN users ON users.id = group_members.user_id WHERE group_id = ?", 
        (group_id,)
    ).fetchall()
    
    user_map = {row['user_id']: row['username'] for row in members}
    balances = {row['user_id']: 0.0 for row in members}
    
    # 2. Calculate Balances
    expenses = conn.execute("SELECT * FROM group_expenses WHERE group_id = ?", (group_id,)).fetchall()
    
    for exp in expenses:
        # HANDLE SETTLEMENTS (The "Done Box" payments)
        if exp['description'] == 'Settlement':
            # In a settlement, the Payer (Debtor) is paying the Split User (Creditor)
            # We need to find who this payment was sent TO
            splits = conn.execute("SELECT user_id FROM expense_splits WHERE expense_id = ?", (exp['id'],)).fetchall()
            if splits:
                receiver_id = splits[0]['user_id']
                # Payer (Debtor) gave money, so their balance increases (becomes less negative)
                if exp['payer_id'] in balances:
                    balances[exp['payer_id']] += exp['amount']
                # Receiver (Creditor) got money, so their balance decreases (becomes less positive/owed)
                if receiver_id in balances:
                    balances[receiver_id] -= exp['amount']
        # HANDLE NORMAL EXPENSES
        else:
            # Payer gets credit (+)
            if exp['payer_id'] in balances:
                balances[exp['payer_id']] += exp['amount']
            
            # Splitters get debit (-)
            splits = conn.execute("SELECT user_id, amount_owed FROM expense_splits WHERE expense_id = ?", (exp['id'],)).fetchall()
            for split in splits:
                if split['user_id'] in balances:
                    balances[split['user_id']] -= split['amount_owed']
    
    # 3. Minimize Transactions
    debtors = []
    creditors = []
    for uid, amount in balances.items():
        if amount < -0.01: 
            debtors.append({'id': uid, 'amount': amount})
        if amount > 0.01: 
            creditors.append({'id': uid, 'amount': amount})
    
    debtors.sort(key=lambda x: x['amount'])
    creditors.sort(key=lambda x: x['amount'], reverse=True)
    
    transactions = []
    d_idx = 0
    c_idx = 0
    
    while d_idx < len(debtors) and c_idx < len(creditors):
        debtor = debtors[d_idx]
        creditor = creditors[c_idx]
        amount = min(abs(debtor['amount']), creditor['amount'])
        
        transactions.append({
            'from_id': debtor['id'],
            'to_id': creditor['id'],
            'from': user_map.get(debtor['id'], 'Unknown'),
            'to': user_map.get(creditor['id'], 'Unknown'),
            'amount': round(amount, 2)
        })
        
        debtor['amount'] += amount
        creditor['amount'] -= amount
        
        if abs(debtor['amount']) < 0.01: 
            d_idx += 1
        if creditor['amount'] < 0.01: 
            c_idx += 1
    
    conn.close()
    return transactions

# --- CATEGORY HELPERS ---
def get_user_categories(user_id):
    """Get all categories for a user, including default categories if none exist"""
    conn = get_db_connection()
    custom_categories = conn.execute(
        'SELECT * FROM categories WHERE user_id = ? ORDER BY name',
        (user_id,)
    ).fetchall()
    conn.close()
    
    # Convert to list of dicts
    categories = [dict(row) for row in custom_categories]
    
    # If no custom categories, return default ones
    if not categories:
        default_categories = ['Food', 'Transportation', 'Entertainment', 'Shopping', 'Bills', 'Healthcare', 'Other']
        return [{'name': cat, 'icon': '💰', 'color': '#6c757d'} for cat in default_categories]
    
    return categories

def process_recurring_expenses(user_id):
    """
    Lazy checks for recurring expenses that are due.
    If today >= next_due_date, it creates a new expense entry and updates the date.
    """
    conn = get_db_connection()
    today = datetime.now().date()
    
    # Find active recurring expenses that are due
    due_expenses = conn.execute('''
        SELECT * FROM expenses 
        WHERE user_id = ? 
        AND is_recurring = 1 
        AND next_due_date <= ?
    ''', (user_id, today.strftime('%Y-%m-%d'))).fetchall()
    
    added_count = 0
    
    for exp in due_expenses:
        # 1. Create the NEW transaction for this month
        current_due_date = datetime.strptime(exp['next_due_date'], '%Y-%m-%d').date()
        
        conn.execute('''
            INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date, is_recurring, frequency, next_due_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL)
        ''', (
            user_id, 
            exp['amount'], 
            exp['currency'], 
            exp['amount_usd'], 
            exp['category'], 
            f"{exp['description']} (Auto-generated)", 
            current_due_date.strftime('%Y-%m-%d'),
            # The new entry is NOT a master recurring trigger itself
            exp['frequency']
        ))
        
        # 2. Update the "Master" expense to the NEXT due date
        next_date = current_due_date
        if exp['frequency'] == 'monthly':
            # Add 1 month (handle year rollover)
            month = next_date.month + 1
            year = next_date.year
            if month > 12:
                month = 1
                year += 1
            # Handle short months (e.g., Jan 31 -> Feb 28)
            try:
                next_date = next_date.replace(year=year, month=month)
            except ValueError:
                # If day is out of range (e.g. 30th Feb), go to last day of month
                import calendar
                last_day = calendar.monthrange(year, month)[1]
                next_date = next_date.replace(year=year, month=month, day=last_day)
                
        elif exp['frequency'] == 'yearly':
            next_date = next_date.replace(year=next_date.year + 1)
        elif exp['frequency'] == 'weekly':
            next_date = next_date + timedelta(days=7)
            
        conn.execute('UPDATE expenses SET next_due_date = ? WHERE id = ?', 
                     (next_date.strftime('%Y-%m-%d'), exp['id']))
        
        added_count += 1
        
    conn.commit()
    conn.close()
    return added_count

def get_category_by_id(category_id, user_id):
    """Get a specific category by ID for the given user"""
    conn = get_db_connection()
    category = conn.execute(
        'SELECT * FROM categories WHERE id = ? AND user_id = ?',
        (category_id, user_id)
    ).fetchone()
    conn.close()
    return category

# --- API ROUTES ---

@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    """
    User Registration
    ---
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            username:
              type: string
            email:
              type: string
            password:
              type: string
    responses:
      201:
        description: User created successfully
      400:
        description: Invalid input or user already exists
    """
    data = request.get_json()
    username = data.get('username', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '')

    if not username or not email or not password:
        return api_response(success=False, message='Missing fields', code=400)

    conn = get_db_connection()
    try:
        hashed_password = generate_password_hash(password)
        conn.execute(
            'INSERT INTO users (username, email, password) VALUES (?, ?, ?)',
            (username, email, hashed_password)
        )
        conn.commit()
        return api_response(message='User created successfully', code=201)
    except sqlite3.IntegrityError:
        return api_response(success=False, message='Username or email already exists', code=400)
    finally:
        conn.close()

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    """
    User Login
    ---
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            username:
              type: string
            password:
              type: string
    responses:
      200:
        description: Login successful, returns JWT
      401:
        description: Invalid credentials
    """
    data = request.get_json()
    username = data.get('username', '')
    password = data.get('password', '')

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()

    if user and check_password_hash(user['password'], password):
        token = jwt.encode({
            'user_id': user['id'],
            'exp': datetime.utcnow() + timedelta(hours=24)
        }, app.config['JWT_SECRET'], algorithm=app.config['JWT_ALGORITHM'])
        
        return api_response(data={'token': token, 'username': user['username']})
    
    return api_response(success=False, message='Invalid credentials', code=401)

@app.route('/api/expenses', methods=['GET'])
@token_required
def api_get_expenses(current_user_id):
    """
    Get all expenses for the current user
    ---
    security:
      - Bearer: []
    responses:
      200:
        description: A list of expenses
    """
    conn = get_db_connection()
    expenses = conn.execute('SELECT * FROM expenses WHERE user_id = ? ORDER BY date DESC', (current_user_id,)).fetchall()
    conn.close()
    return api_response(data=[dict(exp) for exp in expenses])

@app.route('/api/expenses', methods=['POST'])
@token_required
def api_add_expense(current_user_id):
    """
    Add a new expense
    ---
    security:
      - Bearer: []
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            amount:
              type: number
            currency:
              type: string
            category:
              type: string
            description:
              type: string
            date:
              type: string
    responses:
      201:
        description: Expense created successfully
    """
    data = request.get_json()
    amount = float(data.get('amount', 0))
    currency = data.get('currency', 'USD')
    category = data.get('category', 'Other')
    description = data.get('description', '')
    date = data.get('date', datetime.now().strftime('%Y-%m-%d'))

    amount_usd = convert_to_usd(amount, currency)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date) 
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (current_user_id, amount, currency, amount_usd, category, description, date)
    )
    conn.commit()
    expense_id = cursor.lastrowid
    conn.close()

    return api_response(data={'id': expense_id}, message='Expense created successfully', code=201)

@app.route('/api/expenses/<int:expense_id>', methods=['PUT'])
@token_required
def api_update_expense(current_user_id, expense_id):
    """
    Update an existing expense
    ---
    security:
      - Bearer: []
    parameters:
      - name: expense_id
        in: path
        required: true
        type: integer
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            amount:
              type: number
            currency:
              type: string
            category:
              type: string
            description:
              type: string
            date:
              type: string
    responses:
      200:
        description: Expense updated successfully
    """
    data = request.get_json()
    conn = get_db_connection()
    
    # Check ownership
    expense = conn.execute('SELECT * FROM expenses WHERE id = ? AND user_id = ?', (expense_id, current_user_id)).fetchone()
    if not expense:
        conn.close()
        return api_response(success=False, message='Expense not found', code=404)

    amount = float(data.get('amount', expense['amount']))
    currency = data.get('currency', expense['currency'])
    category = data.get('category', expense['category'])
    description = data.get('description', expense['description'])
    date = data.get('date', expense['date'])
    amount_usd = convert_to_usd(amount, currency)

    conn.execute(
        '''UPDATE expenses SET amount=?, currency=?, amount_usd=?, category=?, description=?, date=? 
           WHERE id=? AND user_id=?''',
        (amount, currency, amount_usd, category, description, date, expense_id, current_user_id)
    )
    conn.commit()
    conn.close()
    return api_response(message='Expense updated successfully')

@app.route('/api/expenses/<int:expense_id>', methods=['DELETE'])
@token_required
def api_delete_expense(current_user_id, expense_id):
    """
    Delete an expense
    ---
    security:
      - Bearer: []
    parameters:
      - name: expense_id
        in: path
        required: true
        type: integer
    responses:
      200:
        description: Expense deleted successfully
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM expenses WHERE id = ? AND user_id = ?', (expense_id, current_user_id))
    if cursor.rowcount == 0:
        conn.close()
        return api_response(success=False, message='Expense not found', code=404)
    conn.commit()
    conn.close()
    return api_response(message='Expense deleted successfully')

@app.route('/api/budgets', methods=['GET'])
@token_required
def api_get_budgets(current_user_id):
    """
    Get all budgets for the current user
    ---
    security:
      - Bearer: []
    responses:
      200:
        description: A list of budgets
    """
    conn = get_db_connection()
    budgets = conn.execute('SELECT * FROM budgets WHERE user_id = ?', (current_user_id,)).fetchall()
    conn.close()
    return api_response(data=[dict(b) for b in budgets])

@app.route('/api/budgets', methods=['POST'])
@token_required
def api_add_budget(current_user_id):
    """
    Add a new budget
    ---
    security:
      - Bearer: []
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            category:
              type: string
            amount:
              type: number
            currency:
              type: string
            period:
              type: string
            start_date:
              type: string
    responses:
      201:
        description: Budget created successfully
    """
    data = request.get_json()
    category = data.get('category')
    amount = float(data.get('amount', 0))
    currency = data.get('currency', 'USD')
    period = data.get('period', 'monthly')
    start_date = data.get('start_date', datetime.now().strftime('%Y-%m-%d'))
    amount_usd = convert_to_usd(amount, currency)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''INSERT INTO budgets (user_id, category, amount, currency, amount_usd, period, start_date)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (current_user_id, category, amount, currency, amount_usd, period, start_date)
    )
    conn.commit()
    budget_id = cursor.lastrowid
    conn.close()
    return api_response(data={'id': budget_id}, message='Budget created successfully', code=201)

@app.route('/api/categories', methods=['GET'])
@token_required
def api_get_categories(current_user_id):
    """
    Get all categories for the current user
    ---
    security:
      - Bearer: []
    responses:
      200:
        description: A list of categories
    """
    categories = get_user_categories(current_user_id)
    return api_response(data=categories)

@app.route('/api/categories', methods=['POST'])
@token_required
def api_add_category(current_user_id, ):
    """
    Add a new category
    ---
    security:
      - Bearer: []
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            name:
              type: string
            icon:
              type: string
            color:
              type: string
    responses:
      201:
        description: Category created successfully
    """
    data = request.get_json()
    name = data.get('name', '').strip()
    icon = data.get('icon', '💰')
    color = data.get('color', '#6c757d')

    if not name:
        return api_response(success=False, message='Name is required', code=400)

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO categories (user_id, name, icon, color) VALUES (?, ?, ?, ?)',
            (current_user_id, name, icon, color)
        )
        conn.commit()
        return api_response(data={'id': cursor.lastrowid}, message='Category created successfully', code=201)
    except sqlite3.IntegrityError:
        return api_response(success=False, message='Category already exists', code=400)
    finally:
        conn.close()

@app.route('/api/groups', methods=['GET'])
@token_required
def api_get_groups(current_user_id):
    """
    Get all groups the current user belongs to
    ---
    security:
      - Bearer: []
    responses:
      200:
        description: A list of groups
    """
    conn = get_db_connection()
    user_groups = conn.execute('''
        SELECT g.id, g.name, COUNT(m.user_id) as member_count 
        FROM groups g
        JOIN group_members m ON g.id = m.group_id
        WHERE m.user_id = ?
        GROUP BY g.id
    ''', (current_user_id,)).fetchall()
    conn.close()
    return api_response(data=[dict(g) for g in user_groups])

@app.route('/api/groups', methods=['POST'])
@token_required
def api_create_group(current_user_id):
    """
    Create a new group
    ---
    security:
      - Bearer: []
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            name:
              type: string
    responses:
      201:
        description: Group created successfully
    """
    data = request.get_json()
    group_name = data.get('name')
    if not group_name:
        return api_response(success=False, message='Group name is required', code=400)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO groups (name, created_by, created_at) VALUES (?, ?, ?)',
                   (group_name, current_user_id, datetime.now()))
    group_id = cursor.lastrowid
    
    # Add creator as first member
    cursor.execute('INSERT INTO group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)',
                   (group_id, current_user_id, datetime.now()))
    conn.commit()
    conn.close()
    return api_response(data={'id': group_id}, message='Group created successfully', code=201)

# --- ROUTES ---

@app.route('/set_currency', methods=['POST'])
def set_currency():
    currency = request.form.get('currency')
    if currency:
        session['currency'] = currency
    return redirect(url_for('dashboard'))

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

# --- 2FA ROUTES ---

@app.route('/signup', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def signup():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not username or not email or not password:
            flash('Please fill in all fields!')
            return render_template('signup.html')
        
        if password != confirm_password:
            flash('Passwords do not match!')
            return render_template('signup.html')
            
        conn = get_db_connection()
        try:
            hashed_password = generate_password_hash(password)
            # Generate 2FA Secret immediately upon signup
            totp_secret = pyotp.random_base32()
            
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO users (username, email, password, totp_secret) VALUES (?, ?, ?, ?)',
                (username, email, hashed_password, totp_secret)
            )
            conn.commit()
            user_id = cursor.lastrowid
            
            # Direct user to 2FA Setup instead of login
            session['pre_2fa_id'] = user_id
            flash('Account created! Please set up Two-Factor Authentication.')
            return redirect(url_for('setup_2fa'))
            
        except sqlite3.IntegrityError as e:
            if 'username' in str(e):
                flash('Username already exists!')
            else:
                flash('Email already exists!')
        finally:
            conn.close()
    
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        
        if user and check_password_hash(user['password'], password):
            # Store ID in a temporary session variable
            session['pre_2fa_id'] = user['id']
            
            # If user has no secret (legacy user), force setup. Otherwise verify.
            if not user['totp_secret']:
                return redirect(url_for('setup_2fa'))
            else:
                return redirect(url_for('verify_2fa'))
        else:
            flash('Invalid credentials!')
            
    return render_template('login.html')

@app.route('/setup_2fa')
def setup_2fa():
    if 'pre_2fa_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['pre_2fa_id'],)).fetchone()
    
    # Use existing secret or generate new one if missing
    secret = user['totp_secret']
    if not secret:
        secret = pyotp.random_base32()
        conn.execute('UPDATE users SET totp_secret = ? WHERE id = ?', (secret, user['id']))
        conn.commit()
    conn.close()
    
    # Generate QR Code
    totp_uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=user['email'], 
        issuer_name='ExpenseTracker'
    )
    
    img = qrcode.make(totp_uri)
    buf = io.BytesIO()
    img.save(buf)
    buf.seek(0)
    qr_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    
    return render_template('setup_2fa.html', qr_code=qr_b64, secret=secret)

@app.route('/verify_2fa', methods=['GET', 'POST'])
def verify_2fa():
    if 'pre_2fa_id' not in session:
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        token = request.form.get('token')
        
        conn = get_db_connection()
        user = conn.execute('SELECT * FROM users WHERE id = ?', (session['pre_2fa_id'],)).fetchone()
        conn.close()
        
        totp = pyotp.TOTP(user['totp_secret'])
        
        if totp.verify(token):
            # Success: Elevate to full session
            session['user_id'] = session['pre_2fa_id']
            session['username'] = user['username']
# Clean up the temp session ID
            session.pop('pre_2fa_id', None)

            # --- START NEW CODE: Trigger Recurring Check ---
            # Now that 2FA is passed, we run the check
            count = process_recurring_expenses(user['id'])
            
            if count > 0:
                flash(f'Logged in successfully! Auto-added {count} recurring expense(s).')
            else:
                flash('Logged in successfully!')
            # --- END NEW CODE ---
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid 2FA Token. Please try again.')
            
    return render_template('verify_2fa.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully!')
    return redirect(url_for('index'))

# --- CATEGORY ROUTES ---

@app.route('/categories')
def categories():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_categories = get_user_categories(session['user_id'])
    return render_template('categories.html', categories=user_categories)

@app.route('/add_category', methods=['GET', 'POST'])
def add_category():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        name = request.form['name'].strip()
        icon = request.form.get('icon', '💰')
        color = request.form.get('color', '#6c757d')
        
        if not name:
            flash('Category name is required!')
            return render_template('add_category.html')
        
        conn = get_db_connection()
        try:
            conn.execute(
                'INSERT INTO categories (user_id, name, icon, color) VALUES (?, ?, ?, ?)',
                (session['user_id'], name, icon, color)
            )
            conn.commit()
            flash('Category added successfully!')
            return redirect(url_for('categories'))
        except sqlite3.IntegrityError:
            flash('A category with this name already exists!')
        finally:
            conn.close()
    
    return render_template('add_category.html')

@app.route('/edit_category/<int:category_id>', methods=['GET', 'POST'])
def edit_category(category_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    category = get_category_by_id(category_id, session['user_id'])
    if not category:
        flash('Category not found!')
        return redirect(url_for('categories'))
    
    if request.method == 'POST':
        name = request.form['name'].strip()
        icon = request.form.get('icon', category['icon'])
        color = request.form.get('color', category['color'])
        
        if not name:
            flash('Category name is required!')
            return render_template('edit_category.html', category=category)
        
        conn = get_db_connection()
        try:
            # Update category name in expenses if name changed
            if name != category['name']:
                conn.execute(
                    'UPDATE expenses SET category = ? WHERE user_id = ? AND category = ?',
                    (name, session['user_id'], category['name'])
                )
                conn.execute(
                    'UPDATE budgets SET category = ? WHERE user_id = ? AND category = ?',
                    (name, session['user_id'], category['name'])
                )
            
            conn.execute(
                'UPDATE categories SET name = ?, icon = ?, color = ? WHERE id = ? AND user_id = ?',
                (name, icon, color, category_id, session['user_id'])
            )
            conn.commit()
            flash('Category updated successfully!')
            return redirect(url_for('categories'))
        except sqlite3.IntegrityError:
            flash('A category with this name already exists!')
        finally:
            conn.close()
    
    return render_template('edit_category.html', category=category)

@app.route('/delete_category/<int:category_id>')
def delete_category(category_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    category = get_category_by_id(category_id, session['user_id'])
    if not category:
        flash('Category not found!')
        return redirect(url_for('categories'))
    
    # Check if category has expenses
    conn = get_db_connection()
    expense_count = conn.execute(
        'SELECT COUNT(*) FROM expenses WHERE user_id = ? AND category = ?',
        (session['user_id'], category['name'])
    ).fetchone()[0]
    
    if expense_count > 0:
        conn.close()
        flash(f'Cannot delete "{category["name"]}" - it has {expense_count} expense(s). Please reassign them first.')
        return redirect(url_for('categories'))
    
    conn.execute('DELETE FROM categories WHERE id = ? AND user_id = ?', (category_id, session['user_id']))
    conn.commit()
    conn.close()
    
    flash('Category deleted successfully!')
    return redirect(url_for('categories'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    display_currency = session.get('currency', 'INR')
    user_id = session['user_id']

    # Total Expenses
    total_expenses_usd = conn.execute(
        'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id = ?', 
        (user_id,)
    ).fetchone()[0]
    total_expenses = convert_from_usd(total_expenses_usd, display_currency)

    # Monthly Expenses
    current_month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
    monthly_expenses_usd = conn.execute(
        'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id = ? AND date >= ?',
        (user_id, current_month_start)
    ).fetchone()[0]
    monthly_expenses = convert_from_usd(monthly_expenses_usd, display_currency)

    # Recent Expenses
    rows = conn.execute(
        'SELECT * FROM expenses WHERE user_id = ? ORDER BY date DESC LIMIT 5',
        (user_id,)
    ).fetchall()
    
    recent_expenses = []
    for row in rows:
        exp = dict(row)
        exp['amount_display'] = convert_from_usd(exp['amount_usd'], display_currency)
        recent_expenses.append(exp)

    # Budgets
    budgets = conn.execute('SELECT * FROM budgets WHERE user_id = ?', (user_id,)).fetchall()
    total_budget_usd = 0
    total_budget_spent_usd = 0
    budget_alerts = []

    for budget in budgets:
        budget_amount_usd = float(budget['amount_usd'])
        total_budget_usd += budget_amount_usd

        # Determine start date based on period
        if budget['period'] == 'monthly':
            start_date = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        elif budget['period'] == 'weekly':
            start_date = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
        else: # yearly
            start_date = datetime.now().replace(month=1, day=1).strftime('%Y-%m-%d')
        
        end_date = datetime.now().strftime('%Y-%m-%d')

        actual_spending_usd = conn.execute(
            '''SELECT COALESCE(SUM(amount_usd), 0) FROM expenses 
               WHERE user_id = ? AND category = ? AND date BETWEEN ? AND ?''',
            (user_id, budget['category'], start_date, end_date)
        ).fetchone()[0]

        total_budget_spent_usd += actual_spending_usd
        
        percentage = (actual_spending_usd / budget_amount_usd * 100) if budget_amount_usd > 0 else 0
        remaining_usd = budget_amount_usd - actual_spending_usd

        if percentage >= 80:
            budget_alerts.append({
                'category': budget['category'],
                'status': 'exceeded' if percentage >= 100 else 'warning',
                'percentage': round(percentage, 1),
                'remaining': convert_from_usd(remaining_usd, display_currency)
            })

    conn.close()
    
    total_budget = convert_from_usd(total_budget_usd, display_currency)
    total_budget_spent = convert_from_usd(total_budget_spent_usd, display_currency)

    return render_template('dashboard.html',
        total_expenses=total_expenses,
        monthly_expenses=monthly_expenses,
        total_budget=total_budget,
        total_budget_spent=total_budget_spent,
        recent_expenses=recent_expenses,
        budget_alerts=budget_alerts,
        currency=display_currency
    )

@app.route('/expenses')
def expenses():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    # 1. Fetch into 'raw_expenses'
    raw_expenses = conn.execute(
        'SELECT * FROM expenses WHERE user_id = ? ORDER BY date DESC', 
        (session['user_id'],)
    ).fetchall()
    conn.close()

    # 2. Decrypt into 'expenses_list'
    expenses_list = []
    for row in raw_expenses:
        r = dict(row)
        r['description'] = decrypt_data(r['description'])
        expenses_list.append(r)
    
    user_categories = get_user_categories(session['user_id'])
    return render_template('expenses.html', expenses=expenses_list, categories=user_categories)

@app.route('/search_expenses')
def search_expenses():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_categories = get_user_categories(session['user_id'])

    keyword = request.args.get('keyword', '').lower()
    
    # Get filter parameters
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    categories_param = request.args.get('categories', '')
    amount_min = request.args.get('amount_min', '')
    amount_max = request.args.get('amount_max', '')
    sort_by = request.args.get('sort_by', 'date')
    sort_order = request.args.get('sort_order', 'desc')
    
    # Build dynamic query
    query = 'SELECT * FROM expenses WHERE user_id = ?'
    params = [session['user_id']]
    
    if date_from:
        query += ' AND date >= ?'
        params.append(date_from)
    if date_to:
        query += ' AND date <= ?'
        params.append(date_to)
    
    if categories_param:
        selected_categories = [c.strip() for c in categories_param.split(',') if c.strip()]
        if selected_categories:
            placeholders = ','.join(['?' for _ in selected_categories])
            query += f' AND category IN ({placeholders})'
            params.extend(selected_categories)
    
    if amount_min:
        try:
            min_usd = convert_to_usd(float(amount_min), session.get('currency', 'USD'))
            query += ' AND amount_usd >= ?'
            params.append(min_usd)
        except ValueError:
            pass
            
    if amount_max:
        try:
            max_usd = convert_to_usd(float(amount_max), session.get('currency', 'USD'))
            query += ' AND amount_usd <= ?'
            params.append(max_usd)
        except ValueError:
            pass
    
    # Sorting
    valid_sort_columns = {'date': 'date', 'amount': 'amount_usd', 'category': 'category'}
    sort_column = valid_sort_columns.get(sort_by, 'date')
    sort_direction = 'ASC' if sort_order.lower() == 'asc' else 'DESC'
    query += f' ORDER BY {sort_column} {sort_direction}'
    
    conn = get_db_connection()
    raw_expenses = conn.execute(query, params).fetchall()
    conn.close()
    
    # Decrypt and Filter in Python
    expenses_list = []
    search_term = keyword.lower() if keyword else None
    
    for row in raw_expenses:
        exp = dict(row)
        exp['description'] = decrypt_data(exp['description'])
        
        if search_term:
            if search_term in exp['description'].lower():
                expenses_list.append(exp)
        else:
            expenses_list.append(exp)
    
    filters = {
        'date_from': date_from,
        'date_to': date_to,
        'categories': categories_param,
        'amount_min': amount_min,
        'amount_max': amount_max,
        'keyword': keyword,
        'sort_by': sort_by,
        'sort_order': sort_order
    }
    
    return render_template('expenses.html', expenses=expenses_list, categories=user_categories, filters=filters, is_filtered=True)

@app.route('/add_expense', methods=['GET', 'POST'])
def add_expense():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        amount = float(request.form['amount'])
        category = request.form['category']
        currency = request.form['currency']
        description = request.form['description']
        date = request.form['date'] or datetime.now().strftime('%Y-%m-%d')
        
        # Recurring Logic
        is_recurring = 1 if 'is_recurring' in request.form else 0
        frequency = request.form.get('frequency', 'monthly')
        
        # If recurring, the "next due date" starts 1 cycle from the entered date
        # OR we can set it to the entered date if we want the *next* one to be tracked.
        # Usually, if I add a bill today, I want the system to remind me *next* month.
        next_due_date = None
        if is_recurring:
            # Simple logic: If I pay today, next due is next month
            dt = datetime.strptime(date, '%Y-%m-%d')
            if frequency == 'monthly':
                 # (Add month logic same as above helper)
                month = dt.month + 1
                year = dt.year
                if month > 12:
                    month = 1
                    year += 1
                try:
                    next_due_date = dt.replace(year=year, month=month).strftime('%Y-%m-%d')
                except ValueError:
                    import calendar
                    last_day = calendar.monthrange(year, month)[1]
                    next_due_date = dt.replace(year=year, month=month, day=last_day).strftime('%Y-%m-%d')
            elif frequency == 'weekly':
                next_due_date = (dt + timedelta(days=7)).strftime('%Y-%m-%d')
            elif frequency == 'yearly':
                next_due_date = dt.replace(year=dt.year + 1).strftime('%Y-%m-%d')

        amount_usd = convert_to_usd(amount, currency)

        raw_description = request.form['description']
        description = encrypt_data(raw_description)

        conn = get_db_connection()
        conn.execute(
            '''INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date, is_recurring, frequency, next_due_date) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (session['user_id'], amount, currency, amount_usd, category, description, date, is_recurring, frequency, next_due_date)
        )
        conn.commit()
        conn.close()
        
        flash('Expense added successfully!')
        return redirect(url_for('expenses'))
    
    user_categories = get_user_categories(session['user_id'])
    return render_template('add_expense.html', categories=user_categories)

@app.route('/edit_expense/<int:expense_id>', methods=['GET', 'POST'])
def edit_expense(expense_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()

    if request.method == 'POST':
        amount = float(request.form['amount'])
        currency = request.form.get('currency')
        category = request.form['category']
        description = request.form['description']
        date = request.form['date'] or datetime.now().strftime('%Y-%m-%d')
        amount_usd = convert_to_usd(amount, currency)

        raw_description = request.form['description']
        description = encrypt_data(raw_description)

        conn.execute(
            '''UPDATE expenses SET amount=?, currency=?, amount_usd=?, category=?, description=?, date=? 
               WHERE id=? AND user_id=?''',
            (amount, currency, amount_usd, category, description, date, expense_id, session['user_id'])
        )
        conn.commit()
        conn.close()
        flash('Expense updated successfully!')
        return redirect(url_for('expenses'))

    expense = conn.execute(
        'SELECT * FROM expenses WHERE id=? AND user_id=?', (expense_id, session['user_id'])
    ).fetchone()
    conn.close()

    if not expense:
        flash('Expense not found!')
        return redirect(url_for('expenses'))
    
    expense_dict = dict(expense)
    expense_dict['description'] = decrypt_data(expense['description'])

    user_categories = get_user_categories(session['user_id'])
    return render_template('edit_expense.html', expense=expense_dict, categories=user_categories, selected_currency=expense_dict['currency'])

@app.route('/delete_expense/<int:expense_id>')
def delete_expense(expense_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    conn.execute(
        'DELETE FROM expenses WHERE id = ? AND user_id = ?',
        (expense_id, session['user_id'])
    )
    conn.commit()
    conn.close()
    
    flash('Expense deleted successfully!')
    return redirect(url_for('expenses'))

@app.route('/analytics')
def analytics():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    display_currency = session.get('currency', 'INR')
    user_id = session['user_id']
    
    # Get time range parameters
    time_range = request.args.get('range', '7')  # 7, 30, 90, 365, or 'custom'
    custom_from = request.args.get('from', '')
    custom_to = request.args.get('to', '')
    
    # Calculate date range
    end_date = datetime.now()
    if time_range == 'custom' and custom_from and custom_to:
        start_date = datetime.strptime(custom_from, '%Y-%m-%d')
        end_date = datetime.strptime(custom_to, '%Y-%m-%d')
        days_count = (end_date - start_date).days + 1
    else:
        days = int(time_range) if time_range.isdigit() else 7
        start_date = end_date - timedelta(days=days-1)
        days_count = days
    
    # --- DAILY SPENDING TREND ---
    daily_labels = []
    daily_data = []
    
    for i in range(days_count):
        current_date = (start_date + timedelta(days=i)).strftime('%Y-%m-%d')
        daily_labels.append((start_date + timedelta(days=i)).strftime('%b %d'))
        
        total_usd = conn.execute(
            'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id=? AND date=?',
            (user_id, current_date)
        ).fetchone()[0]
        daily_data.append(round(convert_from_usd(total_usd, display_currency), 2))
    
    # --- CATEGORY BREAKDOWN ---
    categories_data = conn.execute(
        '''SELECT category, COALESCE(SUM(amount_usd), 0) as total_usd 
           FROM expenses WHERE user_id=? AND date BETWEEN ? AND ?
           GROUP BY category''',
        (user_id, start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))
    ).fetchall()
    
    category_labels = [row['category'] for row in categories_data]
    category_totals = [round(convert_from_usd(row['total_usd'], display_currency), 2) for row in categories_data]
    
    # --- BUDGET PERFORMANCE ---
    budgets = conn.execute('SELECT * FROM budgets WHERE user_id = ?', (user_id,)).fetchall()
    budget_labels = []
    budget_allocated = []
    budget_spent = []
    
    for budget in budgets:
        budget_labels.append(budget['category'])
        budget_allocated.append(round(convert_from_usd(budget['amount_usd'], display_currency), 2))
        
        # Calculate period
        if budget['period'] == 'monthly':
            period_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        elif budget['period'] == 'weekly':
            period_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
        else:  # yearly
            period_start = datetime.now().replace(month=1, day=1).strftime('%Y-%m-%d')
        
        actual_usd = conn.execute(
            '''SELECT COALESCE(SUM(amount_usd), 0) FROM expenses 
               WHERE user_id = ? AND category = ? AND date >= ?''',
            (user_id, budget['category'], period_start)
        ).fetchone()[0]
        budget_spent.append(round(convert_from_usd(actual_usd, display_currency), 2))
    
    # --- COMPARATIVE ANALYTICS (Month-over-Month) ---
    current_month_start = datetime.now().replace(day=1)
    current_month_end = datetime.now()
    last_month_end = current_month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    
    current_month_total_usd = conn.execute(
        'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id=? AND date BETWEEN ? AND ?',
        (user_id, current_month_start.strftime('%Y-%m-%d'), current_month_end.strftime('%Y-%m-%d'))
    ).fetchone()[0]
    
    last_month_total_usd = conn.execute(
        'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id=? AND date BETWEEN ? AND ?',
        (user_id, last_month_start.strftime('%Y-%m-%d'), last_month_end.strftime('%Y-%m-%d'))
    ).fetchone()[0]
    
    mom_current = round(convert_from_usd(current_month_total_usd, display_currency), 2)
    mom_last = round(convert_from_usd(last_month_total_usd, display_currency), 2)
    mom_change = round(((current_month_total_usd - last_month_total_usd) / last_month_total_usd * 100) if last_month_total_usd > 0 else 0, 1)
    
    # --- YEAR-OVER-YEAR COMPARISON ---
    current_year = datetime.now().year
    last_year = current_year - 1
    
    current_year_total_usd = conn.execute(
        'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id=? AND strftime("%Y", date) = ?',
        (user_id, str(current_year))
    ).fetchone()[0]
    
    last_year_total_usd = conn.execute(
        'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id=? AND strftime("%Y", date) = ?',
        (user_id, str(last_year))
    ).fetchone()[0]
    
    yoy_current = round(convert_from_usd(current_year_total_usd, display_currency), 2)
    yoy_last = round(convert_from_usd(last_year_total_usd, display_currency), 2)
    yoy_change = round(((current_year_total_usd - last_year_total_usd) / last_year_total_usd * 100) if last_year_total_usd > 0 else 0, 1)
    
    # --- SPENDING FORECAST (Next 30 Days) ---
    last_30_days = end_date - timedelta(days=29)
    recent_expenses = conn.execute(
        'SELECT date, COALESCE(SUM(amount_usd), 0) as daily_total FROM expenses WHERE user_id=? AND date BETWEEN ? AND ? GROUP BY date',
        (user_id, last_30_days.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))
    ).fetchall()
    
    if len(recent_expenses) >= 7:
        # Simple moving average forecast
        recent_totals = [row['daily_total'] for row in recent_expenses]
        avg_daily_spend = sum(recent_totals) / len(recent_totals)
        forecast_next_month = round(convert_from_usd(avg_daily_spend * 30, display_currency), 2)
    else:
        forecast_next_month = 0
    
    # --- TRANSACTION ANALYTICS ---
    total_transactions = conn.execute(
        'SELECT COUNT(*) FROM expenses WHERE user_id=? AND date BETWEEN ? AND ?',
        (user_id, start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))
    ).fetchone()[0]
    
    avg_expense_usd = conn.execute(
        'SELECT AVG(amount_usd) FROM expenses WHERE user_id=? AND date BETWEEN ? AND ?',
        (user_id, start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))
    ).fetchone()[0] or 0
    
    avg_expense = round(convert_from_usd(avg_expense_usd, display_currency), 2)
    
    # --- SPENDING PATTERNS (Weekend vs Weekday) ---
    weekend_usd = conn.execute(
        '''SELECT COALESCE(SUM(amount_usd), 0) FROM expenses 
           WHERE user_id=? AND date BETWEEN ? AND ? 
           AND CAST(strftime('%w', date) AS INTEGER) IN (0, 6)''',
        (user_id, start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))
    ).fetchone()[0]
    
    weekday_usd = conn.execute(
        '''SELECT COALESCE(SUM(amount_usd), 0) FROM expenses 
           WHERE user_id=? AND date BETWEEN ? AND ? 
           AND CAST(strftime('%w', date) AS INTEGER) NOT IN (0, 6)''',
        (user_id, start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))
    ).fetchone()[0]
    
    weekend_spending = round(convert_from_usd(weekend_usd, display_currency), 2)
    weekday_spending = round(convert_from_usd(weekday_usd, display_currency), 2)
    
    # --- HEAT MAP DATA (Day of Week) ---
    heatmap_data = [0] * 7  # Sun-Sat
    for day in range(7):
        day_total_usd = conn.execute(
            '''SELECT COALESCE(SUM(amount_usd), 0) FROM expenses 
               WHERE user_id=? AND date BETWEEN ? AND ? 
               AND CAST(strftime('%w', date) AS INTEGER) = ?''',
            (user_id, start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'), day)
        ).fetchone()[0]
        heatmap_data[day] = round(convert_from_usd(day_total_usd, display_currency), 2)
    
    # --- CATEGORY TRENDS (Growth/Decline) ---
    category_trends = []
    for cat_row in categories_data:
        cat = cat_row['category']
        # Compare current period to previous period
        prev_start = start_date - timedelta(days=days_count)
        prev_end = start_date - timedelta(days=1)
        
        prev_total_usd = conn.execute(
            'SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id=? AND category=? AND date BETWEEN ? AND ?',
            (user_id, cat, prev_start.strftime('%Y-%m-%d'), prev_end.strftime('%Y-%m-%d'))
        ).fetchone()[0]
        
        current_total_usd = cat_row['total_usd']
        change_pct = round(((current_total_usd - prev_total_usd) / prev_total_usd * 100) if prev_total_usd > 0 else 0, 1)
        
        category_trends.append({
            'category': cat,
            'change': change_pct,
            'direction': 'up' if change_pct > 0 else 'down' if change_pct < 0 else 'stable'
        })
    
    # --- FINANCIAL HEALTH SCORE (0-100) ---
    # Based on: budget adherence (40%), spending trend (30%), transaction frequency (30%)
    health_score = 100
    
    # Budget adherence
    if budgets:
        over_budget_count = sum(1 for i, b in enumerate(budgets) if i < len(budget_spent) and budget_spent[i] > budget_allocated[i])
        budget_score = max(0, 100 - (over_budget_count / len(budgets) * 100))
        health_score = budget_score * 0.4
    else:
        health_score = 40  # Neutral if no budgets
    
    # Spending trend (lower is better)
    if mom_change < -10:
        health_score += 30  # Decreasing spending
    elif mom_change > 10:
        health_score += 10  # Increasing spending
    else:
        health_score += 20  # Stable
    
    # Transaction discipline (consistent spending)
    if total_transactions > 0:
        avg_daily_transactions = total_transactions / days_count
        if 0.5 <= avg_daily_transactions <= 3:
            health_score += 30  # Good discipline
        else:
            health_score += 15  # Too many or too few
    else:
        health_score += 15
    
    health_score = min(100, max(0, round(health_score)))
    
    conn.close()

    return render_template(
        'analytics.html',
        # Time range
        time_range=time_range,
        custom_from=custom_from,
        custom_to=custom_to,
        # Daily trend
        labels=json.dumps(daily_labels),
        daily_data=json.dumps(daily_data),
        # Category breakdown
        category_labels=json.dumps(category_labels),
        category_totals=json.dumps(category_totals),
        # Budget performance
        budget_labels=json.dumps(budget_labels),
        budget_allocated=json.dumps(budget_allocated),
        budget_spent=json.dumps(budget_spent),
        # Comparative analytics
        mom_current=mom_current,
        mom_last=mom_last,
        mom_change=mom_change,
        yoy_current=yoy_current,
        yoy_last=yoy_last,
        yoy_change=yoy_change,
        # Predictions
        forecast_next_month=forecast_next_month,
        # Statistics
        total_transactions=total_transactions,
        avg_expense=avg_expense,
        weekend_spending=weekend_spending,
        weekday_spending=weekday_spending,
        # Heat map
        heatmap_data=json.dumps(heatmap_data),
        # Category trends
        category_trends=category_trends,
        # Health score
        health_score=health_score,
        # Currency
        currency=display_currency
    )

@app.route('/budgets')
def budgets():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    display_currency = session.get('currency', 'INR')
    budgets_list = conn.execute('SELECT * FROM budgets WHERE user_id=? ORDER BY category', (session['user_id'],)).fetchall()
    
    budgets_with_spending = []
    for budget in budgets_list:
        b_dict = dict(budget)
        amount_usd = float(budget['amount_usd'])
        
        # Period start date
        if budget['period'] == 'monthly':
            start_date = datetime.now().replace(day=1).strftime('%Y-%m-%d')
        elif budget['period'] == 'weekly':
            start_date = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
        else:
            start_date = datetime.now().replace(month=1, day=1).strftime('%Y-%m-%d')
        end_date = datetime.now().strftime('%Y-%m-%d')

        actual_usd = conn.execute(
            '''SELECT COALESCE(SUM(amount_usd), 0) FROM expenses 
               WHERE user_id=? AND category=? AND date BETWEEN ? AND ?''',
            (session['user_id'], budget['category'], start_date, end_date)
        ).fetchone()[0]

        b_dict['actual_spending'] = convert_from_usd(actual_usd, display_currency)
        b_dict['remaining'] = convert_from_usd(amount_usd - actual_usd, display_currency)
        b_dict['amount'] = convert_from_usd(amount_usd, display_currency)
        b_dict['percentage_used'] = round((actual_usd / amount_usd * 100) if amount_usd > 0 else 0, 1)
        budgets_with_spending.append(b_dict)

    conn.close()
    return render_template('budgets.html', budgets=budgets_with_spending, currency=display_currency)

@app.route('/add_budget', methods=['GET', 'POST'])
def add_budget():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        category = request.form['category']
        amount = float(request.form['amount'])
        currency = request.form['currency']
        period = request.form['period']
        start_date = request.form['start_date'] or datetime.now().strftime('%Y-%m-%d')
        amount_usd = convert_to_usd(amount, currency)

        conn = get_db_connection()
        existing = conn.execute(
            'SELECT * FROM budgets WHERE user_id=? AND category=? AND period=?',
            (session['user_id'], category, period)
        ).fetchone()

        if existing:
            flash('Budget already exists for this category and period!')
            conn.close()
            user_categories = get_user_categories(session['user_id'])
            return render_template('add_budget.html', categories=user_categories)

        conn.execute(
            '''INSERT INTO budgets (user_id, category, amount, currency, amount_usd, period, start_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (session['user_id'], category, amount, currency, amount_usd, period, start_date)
        )
        conn.commit()
        conn.close()
        flash('Budget added successfully!')
        return redirect(url_for('budgets'))
    user_categories = get_user_categories(session['user_id'])
    return render_template('add_budget.html', categories=user_categories)

@app.route('/edit_budget/<int:budget_id>', methods=['GET', 'POST'])
def edit_budget(budget_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    if request.method == 'POST':
        amount = float(request.form['amount'])
        currency = request.form.get('currency')
        period = request.form['period']
        start_date = request.form['start_date']
        amount_usd = convert_to_usd(amount, currency)

        conn.execute(
            '''UPDATE budgets SET amount=?, currency=?, amount_usd=?, period=?, start_date=? 
               WHERE id=? AND user_id=?''',
            (amount, currency, amount_usd, period, start_date, budget_id, session['user_id'])
        )
        conn.commit()
        conn.close()
        flash('Budget updated successfully!')
        return redirect(url_for('budgets'))

    budget = conn.execute('SELECT * FROM budgets WHERE id=? AND user_id=?', (budget_id, session['user_id'])).fetchone()
    conn.close()
    if not budget:
        flash('Budget not found!')
        return redirect(url_for('budgets'))

    return render_template('edit_budget.html', budget=budget, selected_currency=budget['currency'])

@app.route('/delete_budget/<int:budget_id>')
def delete_budget(budget_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    budget = conn.execute('SELECT * FROM budgets WHERE id=? AND user_id=?', (budget_id, session['user_id'])).fetchone()
    if not budget:
        flash('Budget not found!')
        conn.close()
        return redirect(url_for('budgets'))
        
    conn.execute('DELETE FROM budgets WHERE id=? AND user_id=?', (budget_id, session['user_id']))
    conn.commit()
    conn.close()
    flash('Budget deleted successfully!')
    return redirect(url_for('budgets'))

@app.route('/export/<string:data_type>/<string:format>')
def export_data(data_type, format):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    conn = get_db_connection()
    
    if data_type == 'expenses':
        query = 'SELECT date, category, description, amount, currency, amount_usd FROM expenses WHERE user_id = ? ORDER BY date DESC'
        filename = f"expenses_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    elif data_type == 'budgets':
        query = 'SELECT category, amount, currency, amount_usd, period, start_date FROM budgets WHERE user_id = ? ORDER BY category'
        filename = f"budgets_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    else:
        return "Invalid data type", 400

    df = pd.read_sql_query(query, conn, params=(user_id,))
    conn.close()

    if format == 'csv':
        output = io.StringIO()
        df.to_csv(output, index=False)
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode()),
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"{filename}.csv"
        )
    
    elif format == 'xlsx':
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name=data_type.capitalize())
        output.seek(0)
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"{filename}.xlsx"
        )
    
    elif format == 'pdf':
        # xhtml2pdf implementation
        output = io.BytesIO()
        
        # Simple HTML Template for PDF
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Helvetica, sans-serif; }}
                table {{ width: 100%; border-collapse: collapse; }}
                th, td {{ padding: 8px; text-align: left; border-bottom: 1px solid #ddd; }}
                th {{ background-color: #f2f2f2; }}
                h2 {{ color: #333; }}
            </style>
        </head>
        <body>
            <h2>{data_type.capitalize()} Report</h2>
            <p>Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            <table>
                <thead>
                    <tr>
                        {''.join([f'<th>{col}</th>' for col in df.columns])}
                    </tr>
                </thead>
                <tbody>
                    {''.join(['<tr>' + ''.join([f'<td>{val}</td>' for val in row]) + '</tr>' for row in df.values])}
                </tbody>
            </table>
        </body>
        </html>
        """
        
        pisa_status = pisa.CreatePDF(
            src=html_content,
            dest=output
        )
        
        if pisa_status.err:
            return f"PDF generation error: {pisa_status.err}", 500
            
        output.seek(0)
        return send_file(
            output,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f"{filename}.pdf"
        )
    
    return "Invalid format", 400

@app.route('/import_expenses', methods=['GET', 'POST'])
def import_expenses():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part')
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '':
            flash('No selected file')
            return redirect(request.url)
        
        if file and file.filename.endswith('.csv'):
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            df = pd.read_csv(stream)
            
            # Save DF to session temporarily (not ideal for large files, but works for this demo)
            # Better to save to a temp file or use a more robust state management
            session['import_df'] = df.to_json()
            return render_template('import_mapping.html', columns=df.columns.tolist())
            
    return render_template('import_expenses.html')

@app.route('/process_import', methods=['POST'])
def process_import():
    if 'user_id' not in session or 'import_df' not in session:
        return redirect(url_for('login'))
    
    mapping = request.form.to_dict()
    df_json = session.pop('import_df')
    df = pd.read_json(io.StringIO(df_json))
    
    conn = get_db_connection()
    try:
        for _, row in df.iterrows():
            amount = float(row[mapping['amount']])
            currency = row[mapping['currency']] if 'currency' in mapping and mapping['currency'] in row else 'USD'
            category = row[mapping['category']] if 'category' in mapping and mapping['category'] in row else 'Miscellaneous'
            description = row[mapping['description']] if 'description' in mapping and mapping['description'] in row else ''
            date = row[mapping['date']] if 'date' in mapping and mapping['date'] in row else datetime.now().strftime('%Y-%m-%d')
            
            amount_usd = convert_to_usd(amount, currency)
            
            conn.execute(
                '''INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date) 
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (session['user_id'], amount, currency, amount_usd, category, description, str(date))
            )
        conn.commit()
        flash('Expenses imported successfully!')
    except Exception as e:
        conn.rollback()
        flash(f'Error importing expenses: {str(e)}')
    finally:
        conn.close()
        
    return redirect(url_for('expenses'))

@app.route('/bulk_delete_expenses', methods=['POST'])
def bulk_delete_expenses():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    expense_ids = request.json.get('ids', [])
    if not expense_ids:
        return jsonify({'success': False, 'error': 'No expenses selected'}), 400
    
    conn = get_db_connection()
    conn.execute(
        f"DELETE FROM expenses WHERE user_id = ? AND id IN ({','.join(['?']*len(expense_ids))})",
        (session['user_id'], *expense_ids)
    )
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/bulk_update_category', methods=['POST'])
def bulk_update_category():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    data = request.json
    expense_ids = data.get('ids', [])
    new_category = data.get('category')
    
    if not expense_ids or not new_category:
        return jsonify({'success': False, 'error': 'Missing data'}), 400
    
    conn = get_db_connection()
    conn.execute(
        f"UPDATE expenses SET category = ? WHERE user_id = ? AND id IN ({','.join(['?']*len(expense_ids))})",
        (new_category, session['user_id'], *expense_ids)
    )
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

# --- CHATBOT LOGIC ---
def get_user_financial_context(user_id):
    conn = get_db_connection()
    
    total_usd = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    
    month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
    monthly_usd = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM expenses WHERE user_id = ? AND date >= ?",
        (user_id, month_start)
    ).fetchone()[0]

    categories = conn.execute(
        "SELECT category, SUM(amount_usd) as total FROM expenses WHERE user_id = ? GROUP BY category",
        (user_id,)
    ).fetchall()
    
    budgets = conn.execute("SELECT category, amount_usd FROM budgets WHERE user_id = ?", (user_id,)).fetchall()
    conn.close()

    return {
        "total_expenses_usd": round(total_usd, 2),
        "monthly_expenses_usd": round(monthly_usd, 2),
        "categories": {row["category"]: round(row["total"], 2) for row in categories},
        "budgets": {row["category"]: round(row["amount_usd"], 2) for row in budgets},
    }

@app.route('/chatbot', methods=['POST'])
def chatbot():
    if 'user_id' not in session:
        return {"reply": "Unauthorized"}, 401

    data = request.get_json()
    user_message = data.get("message", "").strip()

    if not user_message:
        return {"reply": "Please enter a message."}

    context = get_user_financial_context(session['user_id'])
    system_prompt = f"""
    You are a personal finance assistant.
    User data (USD):
    - Total expenses: {context['total_expenses_usd']}
    - Monthly expenses: {context['monthly_expenses_usd']}
    - Category totals: {context['categories']}
    - Budgets: {context['budgets']}
    Rules: Do not invent data. Answer clearly.
    """

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.4,
            max_tokens=300
        )
        return {"reply": response.choices[0].message.content}
    except Exception as e:
        print(e)
        return {"reply": "AI service error. Try again later."}

# ================= NEW: SPLITWISE FEATURES =================

@app.route('/groups')
def groups():
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    user_groups = conn.execute('''
        SELECT g.id, g.name, COUNT(m.user_id) as member_count 
        FROM groups g
        JOIN group_members m ON g.id = m.group_id
        WHERE m.user_id = ?
        GROUP BY g.id
    ''', (session['user_id'],)).fetchall()
    conn.close()
    
    return render_template('groups.html', groups=user_groups)

@app.route('/create_group', methods=['POST'])
def create_group():
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
    group_name = request.form['name']
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('INSERT INTO groups (name, created_by, created_at) VALUES (?, ?, ?)',
                   (group_name, session['user_id'], datetime.now()))
    group_id = cursor.lastrowid
    
    # Add creator as first member with 'owner' role
    cursor.execute('INSERT INTO group_members (group_id, user_id, joined_at, role) VALUES (?, ?, ?, ?)',
                   (group_id, session['user_id'], datetime.now(), 'owner'))
    conn.commit()
    conn.close()
    
    flash('Group created successfully!')
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/group/<int:group_id>')
def group_detail(group_id):
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    
    group = conn.execute('SELECT * FROM groups WHERE id = ?', (group_id,)).fetchone()
    if not group:
        conn.close()
        flash("Group not found.")
        return redirect(url_for('groups'))

    # Access Control
    is_member = conn.execute('SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?',
                              (group_id, session['user_id'])).fetchone()
    if not is_member:
        conn.close()
        flash("You are not a member of this group.")
        return redirect(url_for('groups'))
    
    # Get Creator Username
    creator = conn.execute('SELECT username FROM users WHERE id = ?', (group['created_by'],)).fetchone()
    creator_username = creator['username'] if creator else 'Unknown'
    
    # Get Expenses
    expenses = conn.execute('''
        SELECT ge.*, u.username as payer_name 
        FROM group_expenses ge 
        JOIN users u ON ge.payer_id = u.id 
        WHERE group_id = ? ORDER BY date DESC
    ''', (group_id,)).fetchall()
    
    # Get Members (For 'Paid By' list and display with roles)
    members = conn.execute('''
        SELECT u.id, u.username, u.email, gm.role 
        FROM group_members gm 
        JOIN users u ON gm.user_id = u.id 
        WHERE group_id = ?
    ''', (group_id,)).fetchall()
    
    # Get current user's role in the group
    user_role = get_user_group_role(session['user_id'], group_id)
    
    conn.close()
    
    debts = calculate_group_debts(group_id)
    return render_template('group_detail.html', group=group, expenses=expenses, members=members, debts=debts, creator_username=creator_username, user_role=user_role)

@app.route('/group/<int:group_id>/add_member', methods=['POST'])
@require_group_role('admin', 'owner')
def add_member(group_id):
    
    username = request.form['username']
    new_member_role = request.form.get('role', 'editor')  # Allow admins to assign roles
    
    # Validate role
    valid_roles = ['viewer', 'editor', 'admin', 'owner']
    if new_member_role not in valid_roles:
        new_member_role = 'editor'
    
    conn = get_db_connection()
    
    # NEW LOGIC: Check by username. If not exists, CREATE IT.
    user = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    
    if not user:
        # Create "Ghost" user automatically so we can add them to the bill
        # Using a timestamp to ensure unique email
        dummy_email = f"{username}_{int(time.time())}@placeholder.com"
        dummy_pass = generate_password_hash("placeholder") # They can't login, which is fine
        c = conn.cursor()
        c.execute('INSERT INTO users (username, email, password) VALUES (?, ?, ?)', (username, dummy_email, dummy_pass))
        user_id = c.lastrowid
        conn.commit()
        flash(f'Created new user "{username}" and added to group!')
    else:
        user_id = user['id']
        flash(f'Added "{username}" to group!')
    
    # Add to group with specified role
    try:
        conn.execute('INSERT INTO group_members (group_id, user_id, joined_at, role) VALUES (?, ?, ?, ?)',
                     (group_id, user_id, datetime.now(), new_member_role))
        conn.commit()
    except sqlite3.IntegrityError:
        flash('User already in group.')
    
    conn.close()
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/group/<int:group_id>/add_expense', methods=['POST'])
@require_group_role('editor', 'admin', 'owner')
def add_group_expense(group_id):
    
    amount = float(request.form['amount'])
    desc = request.form['description']
    payer_id = int(request.form['payer_id']) # Now we use the ID from the dropdown
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Insert Expense
    cursor.execute('INSERT INTO group_expenses (group_id, payer_id, amount, description, date) VALUES (?, ?, ?, ?, ?)',
                    (group_id, payer_id, amount, desc, datetime.now()))
    expense_id = cursor.lastrowid
    
    # Split equally among ALL members
    members = conn.execute('SELECT user_id FROM group_members WHERE group_id = ?', (group_id,)).fetchall()
    if members:
        split = amount / len(members)
        for m in members:
            conn.execute('INSERT INTO expense_splits (expense_id, user_id, amount_owed) VALUES (?, ?, ?)',
                         (expense_id, m['user_id'], split))
        conn.commit()
    
    conn.close()
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/group/<int:group_id>/settle_up', methods=['POST'])
@require_group_role('editor', 'admin', 'owner')
def settle_up(group_id):
    
    payer_id = int(request.form['from_id']) # Debtor
    receiver_id = int(request.form['to_id']) # Creditor
    amount = float(request.form['amount']) # Partial or Full amount
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # Record Settlement as an Expense (Payer = Debtor)
    c.execute('INSERT INTO group_expenses (group_id, payer_id, amount, description, date) VALUES (?, ?, ?, ?, ?)',
              (group_id, payer_id, amount, "Settlement", datetime.now()))
    exp_id = c.lastrowid
    
    # Assign the split fully to the Receiver (Creditor)
    # Math: Debtor Paid (+balance), Creditor Received (-balance)
    c.execute('INSERT INTO expense_splits (expense_id, user_id, amount_owed) VALUES (?, ?, ?)',
              (exp_id, receiver_id, amount))
    
    conn.commit()
    conn.close()
    
    flash('Payment recorded!')
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/group/<int:group_id>/delete', methods=['POST'])
@require_group_role('owner')
def delete_group(group_id):
    
    conn = get_db_connection()
    
    # Delete cascade (decorator already verified owner permission)
    conn.execute('DELETE FROM expense_splits WHERE expense_id IN (SELECT id FROM group_expenses WHERE group_id = ?)', (group_id,))
    conn.execute('DELETE FROM group_expenses WHERE group_id = ?', (group_id,))
    conn.execute('DELETE FROM group_members WHERE group_id = ?', (group_id,))
    conn.execute('DELETE FROM groups WHERE id = ?', (group_id,))
    conn.commit()
    conn.close()
    
    flash('Group deleted successfully!')
    return redirect(url_for('groups'))

@app.route('/group/<int:group_id>/expense/<int:expense_id>/delete', methods=['POST'])
def delete_group_expense(group_id, expense_id):
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    
    # Check if user is a member
    member = conn.execute(
        'SELECT role FROM group_members WHERE group_id = ? AND user_id = ?',
        (group_id, session['user_id'])
    ).fetchone()
    
    if not member:
        flash("You are not a member of this group.")
        conn.close()
        return redirect(url_for('groups'))
    
    # Check if expense belongs to group
    expense = conn.execute('SELECT group_id, payer_id FROM group_expenses WHERE id = ?', (expense_id,)).fetchone()
    
    if not expense or expense['group_id'] != group_id:
        flash('Expense not found.')
        conn.close()
        return redirect(url_for('group_detail', group_id=group_id))
    
    # Permission check: payer can always delete their expense, admin/owner can delete any
    user_role = member['role']
    is_payer = expense['payer_id'] == session['user_id']
    is_admin_or_owner = user_role in ['admin', 'owner']
    
    if not (is_payer or is_admin_or_owner):
        flash('Only the payer, admin, or owner can delete this expense.')
        conn.close()
        return redirect(url_for('group_detail', group_id=group_id))
    
    # Delete expense and splits
    conn.execute('DELETE FROM expense_splits WHERE expense_id = ?', (expense_id,))
    conn.execute('DELETE FROM group_expenses WHERE id = ?', (expense_id,))
    conn.commit()
    conn.close()
    
    flash('Expense deleted successfully!')
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/group/<int:group_id>/remove_member/<int:member_id>', methods=['POST'])
@require_group_role('admin', 'owner')
def remove_member(group_id, member_id):
    """Remove a member from group (admin+ only, cannot remove owner)"""
    conn = get_db_connection()
    
    # Check if the member being removed is the owner
    member = conn.execute(
        'SELECT role FROM group_members WHERE group_id = ? AND user_id = ?',
        (group_id, member_id)
    ).fetchone()
    
    if not member:
        flash('Member not found in this group.')
        conn.close()
        return redirect(url_for('group_detail', group_id=group_id))
    
    if member['role'] == 'owner':
        flash('Cannot remove the group owner. Transfer ownership first.')
        conn.close()
        return redirect(url_for('group_detail', group_id=group_id))
    
    # Remove member
    conn.execute('DELETE FROM group_members WHERE group_id = ? AND user_id = ?', (group_id, member_id))
    conn.commit()
    conn.close()
    
    flash('Member removed successfully!')
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/group/<int:group_id>/change_role/<int:member_id>', methods=['POST'])
@require_group_role('owner')
def change_member_role(group_id, member_id):
    """Change a member's role (owner only)"""
    new_role = request.form.get('role')
    
    # Validate role
    valid_roles = ['viewer', 'editor', 'admin', 'owner']
    if new_role not in valid_roles:
        flash('Invalid role selected.')
        return redirect(url_for('group_detail', group_id=group_id))
    
    conn = get_db_connection()
    
    # Cannot change own role
    if member_id == session['user_id']:
        flash('Cannot change your own role. Use transfer ownership instead.')
        conn.close()
        return redirect(url_for('group_detail', group_id=group_id))
    
    # Update role
    conn.execute(
        'UPDATE group_members SET role = ? WHERE group_id = ? AND user_id = ?',
        (new_role, group_id, member_id)
    )
    conn.commit()
    conn.close()
    
    flash(f'Member role updated to {new_role}!')
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/group/<int:group_id>/transfer_ownership/<int:new_owner_id>', methods=['POST'])
@require_group_role('owner')
def transfer_ownership(group_id, new_owner_id):
    """Transfer ownership to another member (owner only)"""
    conn = get_db_connection()
    
    # Check if new owner is a member
    new_owner = conn.execute(
        'SELECT user_id FROM group_members WHERE group_id = ? AND user_id = ?',
        (group_id, new_owner_id)
    ).fetchone()
    
    if not new_owner:
        flash('Selected user is not a member of this group.')
        conn.close()
        return redirect(url_for('group_detail', group_id=group_id))
    
    # Transfer ownership: new member becomes owner, old owner becomes admin
    conn.execute(
        'UPDATE group_members SET role = ? WHERE group_id = ? AND user_id = ?',
        ('admin', group_id, session['user_id'])
    )
    conn.execute(
        'UPDATE group_members SET role = ? WHERE group_id = ? AND user_id = ?',
        ('owner', group_id, new_owner_id)
    )
    
    # Update group creator
    conn.execute(
        'UPDATE groups SET created_by = ? WHERE id = ?',
        (new_owner_id, group_id)
    )
    
    conn.commit()
    conn.close()
    
    flash('Ownership transferred successfully!')
    return redirect(url_for('group_detail', group_id=group_id))

if __name__ == '__main__':
    init_db()
    # Pre-fetch rates
    try:
        _RATES_CACHE["rates"] = _fetch_usd_rates()
        _RATES_CACHE["timestamp"] = time.time()
    except Exception:
        pass
    app.run(debug=True)