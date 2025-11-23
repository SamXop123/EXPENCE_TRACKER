from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import json

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # Change this to a random secret key

def get_db_connection():
    conn = sqlite3.connect('expenses.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            date TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    conn.commit()
    conn.close()

# Routes
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        
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
        except sqlite3.IntegrityError:
            flash('Username or email already exists!')
        finally:
            conn.close()
    
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = get_db_connection()
        user = conn.execute(
            'SELECT * FROM users WHERE username = ?', (username,)
        ).fetchone()
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

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    
    # Get total expenses
    total_expenses = conn.execute(
        'SELECT SUM(amount) FROM expenses WHERE user_id = ?',
        (session['user_id'],)
    ).fetchone()[0] or 0
    
    # Get recent expenses
    recent_expenses = conn.execute('''
        SELECT * FROM expenses 
        WHERE user_id = ? 
        ORDER BY date DESC 
        LIMIT 5
    ''', (session['user_id'],)).fetchall()
    
    conn.close()
    
    return render_template('dashboard.html', 
                         total_expenses=total_expenses,
                         recent_expenses=recent_expenses)

@app.route('/expenses')
def expenses():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    expenses_list = conn.execute('''
        SELECT * FROM expenses 
        WHERE user_id = ? 
        ORDER BY date DESC
    ''', (session['user_id'],)).fetchall()
    conn.close()
    
    return render_template('expenses.html', expenses=expenses_list)

@app.route('/add_expense', methods=['GET', 'POST'])
def add_expense():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        amount = float(request.form['amount'])
        category = request.form['category']
        description = request.form['description']
        date = request.form['date'] or datetime.now().strftime('%Y-%m-%d')
        
        conn = get_db_connection()
        conn.execute(
            'INSERT INTO expenses (user_id, amount, category, description, date) VALUES (?, ?, ?, ?, ?)',
            (session['user_id'], amount, category, description, date)
        )
        conn.commit()
        conn.close()
        
        flash('Expense added successfully!')
        return redirect(url_for('expenses'))
    
    return render_template('add_expense.html')

@app.route('/edit_expense/<int:expense_id>', methods=['GET', 'POST'])
def edit_expense(expense_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    
    if request.method == 'POST':
        amount = float(request.form['amount'])
        category = request.form['category']
        description = request.form['description']
        date = request.form['date']
        
        conn.execute(
            'UPDATE expenses SET amount = ?, category = ?, description = ?, date = ? WHERE id = ? AND user_id = ?',
            (amount, category, description, date, expense_id, session['user_id'])
        )
        conn.commit()
        conn.close()
        
        flash('Expense updated successfully!')
        return redirect(url_for('expenses'))
    
    expense = conn.execute(
        'SELECT * FROM expenses WHERE id = ? AND user_id = ?',
        (expense_id, session['user_id'])
    ).fetchone()
    conn.close()
    
    if not expense:
        flash('Expense not found!')
        return redirect(url_for('expenses'))
    
    return render_template('edit_expense.html', expense=expense)

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
    
    # Get daily spending for last 7 days
    end_date = datetime.now()
    start_date = end_date - timedelta(days=6)
    
    daily_data = []
    labels = []
    
    for i in range(7):
        current_date = (start_date + timedelta(days=i)).strftime('%Y-%m-%d')
        labels.append((start_date + timedelta(days=i)).strftime('%b %d'))
        
        total = conn.execute('''
            SELECT SUM(amount) FROM expenses 
            WHERE user_id = ? AND date = ?
        ''', (session['user_id'], current_date)).fetchone()[0] or 0
        
        daily_data.append(float(total))
    
    # Get category-wise spending
    categories_data = conn.execute('''
        SELECT category, SUM(amount) as total 
        FROM expenses 
        WHERE user_id = ? 
        GROUP BY category
    ''', (session['user_id'],)).fetchall()
    
    category_labels = [row['category'] for row in categories_data]
    category_totals = [float(row['total']) for row in categories_data]
    
    conn.close()
    
    return render_template('analytics.html',
                         labels=json.dumps(labels),
                         daily_data=json.dumps(daily_data),
                         category_labels=json.dumps(category_labels),
                         category_totals=json.dumps(category_totals))

if __name__ == '__main__':
    init_db()
    app.run(debug=True)