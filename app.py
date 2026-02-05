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
def get_db_connection():
    conn = sqlite3.connect('expenses.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    
    # Users Table
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    
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
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
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

def get_category_by_id(category_id, user_id):
    """Get a specific category by ID for the given user"""
    conn = get_db_connection()
    category = conn.execute(
        'SELECT * FROM categories WHERE id = ? AND user_id = ?',
        (category_id, user_id)
    ).fetchone()
    conn.close()
    return category

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

@app.route('/signup', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def signup():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Validation
        if not username or not email or not password:
            flash('Please fill in all fields!')
            return render_template('signup.html')
        
        if password != confirm_password:
            flash('Passwords do not match!')
            return render_template('signup.html')
        
        if len(password) < 4:
            flash('Password must be at least 4 characters!')
            return render_template('signup.html')
        
        conn = get_db_connection()
        try:
            hashed_password = generate_password_hash(password)
            conn.execute(
                'INSERT INTO users (username, email, password) VALUES (?, ?, ?)',
                (username, email, hashed_password)
            )
            conn.commit()
            flash('Account created successfully! Please login.')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError as e:
            if 'username' in str(e):
                flash('Username already exists!')
            elif 'email' in str(e):
                flash('Email already exists!')
            else:
                flash('Signup failed. Please try again.')
        except Exception as e:
            flash(f'Error: {str(e)}')
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
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully!')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials!')
    return render_template('login.html')

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
    expenses_list = conn.execute(
        'SELECT * FROM expenses WHERE user_id = ? ORDER BY date DESC', 
        (session['user_id'],)
    ).fetchall()
    conn.close()
    
    user_categories = get_user_categories(session['user_id'])
    return render_template('expenses.html', expenses=expenses_list, categories=user_categories)

@app.route('/search_expenses')
def search_expenses():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    # Get user categories (FIXED: Defined before use)
    user_categories = get_user_categories(session['user_id'])
    
    # Get filter parameters
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    categories_param = request.args.get('categories', '')
    amount_min = request.args.get('amount_min', '')
    amount_max = request.args.get('amount_max', '')
    keyword = request.args.get('keyword', '')
    sort_by = request.args.get('sort_by', 'date')
    sort_order = request.args.get('sort_order', 'desc')
    
    # Build dynamic query
    query = 'SELECT * FROM expenses WHERE user_id = ?'
    params = [session['user_id']]
    
    # Date range filter
    if date_from:
        query += ' AND date >= ?'
        params.append(date_from)
    if date_to:
        query += ' AND date <= ?'
        params.append(date_to)
    
    # Category filter (multiple categories supported)
    if categories_param:
        selected_categories = [c.strip() for c in categories_param.split(',') if c.strip()]
        if selected_categories:
            placeholders = ','.join(['?' for _ in selected_categories])
            query += f' AND category IN ({placeholders})'
            params.extend(selected_categories)
    
    # Amount range filter (using amount_usd for consistent comparison)
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
    
    # Keyword search in description
    if keyword:
        # [FIX] Ensure keyword is a string
        if not isinstance(keyword, str):
            keyword = str(keyword)
        query += ' AND description LIKE ?'
        params.append(f'%{keyword}%')
    
    # Sorting (validate sort_by to prevent SQL injection)
    valid_sort_columns = {'date': 'date', 'amount': 'amount_usd', 'category': 'category'}
    sort_column = valid_sort_columns.get(sort_by, 'date')
    sort_direction = 'ASC' if sort_order.lower() == 'asc' else 'DESC'
    query += f' ORDER BY {sort_column} {sort_direction}'
    
    conn = get_db_connection()
    expenses_list = conn.execute(query, params).fetchall()
    conn.close()
    
    # Pass filter values back for form persistence
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

        amount_usd = convert_to_usd(amount, currency)

        conn = get_db_connection()
        conn.execute(
            '''INSERT INTO expenses (user_id, amount, currency, amount_usd, category, description, date) 
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (session['user_id'], amount, currency, amount_usd, category, description, date)
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

    user_categories = get_user_categories(session['user_id'])
    return render_template('edit_expense.html', expense=expense, categories=user_categories, selected_currency=expense['currency'])

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
    
    # Add creator as first member
    cursor.execute('INSERT INTO group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)',
                   (group_id, session['user_id'], datetime.now()))
    conn.commit()
    conn.close()
    
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/group/<int:group_id>')
def group_detail(group_id):
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    
    # Access Control
    is_member = conn.execute('SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?',
                              (group_id, session['user_id'])).fetchone()
    if not is_member:
        conn.close()
        flash("You are not a member of this group.")
        return redirect(url_for('groups'))
    
    group = conn.execute('SELECT * FROM groups WHERE id = ?', (group_id,)).fetchone()
    
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
    
    # Get Members (For 'Paid By' list)
    members = conn.execute('''
        SELECT u.id, u.username, u.email 
        FROM group_members gm 
        JOIN users u ON gm.user_id = u.id 
        WHERE group_id = ?
    ''', (group_id,)).fetchall()
    
    conn.close()
    
    debts = calculate_group_debts(group_id)
    return render_template('group_detail.html', group=group, expenses=expenses, members=members, debts=debts, creator_username=creator_username)

@app.route('/group/<int:group_id>/add_member', methods=['POST'])
def add_member(group_id):
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
    username = request.form['username']
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
    
    # Add to group
    try:
        conn.execute('INSERT INTO group_members (group_id, user_id, joined_at) VALUES (?, ?, ?)',
                     (group_id, user_id, datetime.now()))
        conn.commit()
    except sqlite3.IntegrityError:
        flash('User already in group.')
    
    conn.close()
    return redirect(url_for('group_detail', group_id=group_id))

@app.route('/group/<int:group_id>/add_expense', methods=['POST'])
def add_group_expense(group_id):
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
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
def settle_up(group_id):
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
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
def delete_group(group_id):
    if 'user_id' not in session: 
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    
    # Check if user is creator/admin of the group
    group = conn.execute('SELECT created_by FROM groups WHERE id = ?', (group_id,)).fetchone()
    
    if not group or group['created_by'] != session['user_id']:
        flash('Only the group creator can delete this group.')
        conn.close()
        return redirect(url_for('group_detail', group_id=group_id))
    
    # Delete cascade
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
    
    # Check if expense belongs to group and user is creator or payer
    expense = conn.execute('SELECT group_id, payer_id FROM group_expenses WHERE id = ?', (expense_id,)).fetchone()
    
    if not expense or expense['group_id'] != group_id:
        flash('Expense not found.')
        conn.close()
        return redirect(url_for('group_detail', group_id=group_id))
    
    if expense['payer_id'] != session['user_id']:
        group = conn.execute('SELECT created_by FROM groups WHERE id = ?', (group_id,)).fetchone()
        if group['created_by'] != session['user_id']:
            flash('Only the payer or group creator can delete this expense.')
            conn.close()
            return redirect(url_for('group_detail', group_id=group_id))
    
    # Delete expense and splits
    conn.execute('DELETE FROM expense_splits WHERE expense_id = ?', (expense_id,))
    conn.execute('DELETE FROM group_expenses WHERE id = ?', (expense_id,))
    conn.commit()
    conn.close()
    
    flash('Expense deleted successfully!')
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