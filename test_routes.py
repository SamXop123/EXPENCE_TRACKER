from flask import Flask, render_template

app = Flask(__name__)

@app.route('/')
def home():
    return "Home page working!"

@app.route('/signup')
def signup():
    return "Signup page working!"

@app.route('/login')
def login():
    return "Login page working!"

if __name__ == '__main__':
    app.run(debug=True)