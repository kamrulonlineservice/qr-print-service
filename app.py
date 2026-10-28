import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.secret_key = 'kamrul_secret_key'

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///shops.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Shop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_name = db.Column(db.String(150), nullable=False)
    owner_name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.Text, nullable=False)
    upi_id = db.Column(db.String(100), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    subscription_expiry = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(days=3))
    is_paid = db.Column(db.Boolean, default=False)
    today_prints = db.Column(db.Integer, default=0)
    today_income = db.Column(db.Float, default=0.0)

with app.app_context():
    db.create_all()

# ১. হোম পেজ (আসল সোর্স কোডের প্রিমিয়াম ডিজাইন)
@app.route('/')
def index():
    return render_template('index.html')

# ২. শপকিপার রেজিস্ট্রেশন পেজ
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        shop_name = request.form.get('shop_name')
        owner_name = request.form.get('owner_name')
        address = request.form.get('address')
        upi_id = request.form.get('upi_id')
        username = request.form.get('username')
        password = request.form.get('password')

        new_shop = Shop(
            shop_name=shop_name, 
            owner_name=owner_name, 
            address=address, 
            upi_id=upi_id,
            username=username,
            password=password,
            created_at=datetime.utcnow(),
            is_paid=False
        )
        db.session.add(new_shop)
        db.session.commit()
        return redirect(url_for('shop_login'))
    
    return render_template('register.html')

# ৩. শপকিপার লগইন পেজ
@app.route('/login', methods=['GET', 'POST'])
def shop_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        shop = Shop.query.filter_by(username=username, password=password).first()
        if shop:
            session['shop_id'] = shop.id
            return redirect(url_for('shop_dashboard'))
        else:
            return "ভুল ইউজারনেম অথবা পাসওয়ার্ড!"
    return render_template('shop_login.html')

# ৪. শপকিপার ড্যাশবোর্ড (আজকের প্রিন্ট, ইনকাম ও কিউআর কোড)
@app.route('/dashboard')
def shop_dashboard():
    shop_id = session.get('shop_id')
    if not shop_id:
        return redirect(url_for('shop_login'))
    
    shop = Shop.query.get_or_404(shop_id)
    expiry_date = shop.subscription_expiry
    is_trial_active = datetime.utcnow() < expiry_date
    
    if not is_trial_active and not shop.is_paid:
        return redirect(url_for('subscribe', shop_id=shop.id))

    upi_url = f"upi://pay?pa={shop.upi_id}&pn={shop.shop_name}"
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={upi_url}"

    return render_template('shop_dashboard.html', shop=shop, qr_url=qr_url)

# ৫. অ্যাডমিন লগইন ও ড্যাশবোর্ড
@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == 'admin' and password == 'kamrul123':
            return redirect(url_for('admin_dashboard'))
        else:
            return "ভুল অ্যাডমিন পাসওয়ার্ড!"
    return render_template('admin_login.html')

@app.route('/admin/dashboard')
def admin_dashboard():
    shops = Shop.query.all()
    return render_template('admin.html', shops=shops)

@app.route('/admin/approve/<int:shop_id>')
def approve_shop(shop_id):
    shop = Shop.query.get_or_404(shop_id)
    shop.is_paid = not shop.is_paid
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/subscribe/<int:shop_id>')
def subscribe(shop_id):
    shop = Shop.query.get_or_404(shop_id)
    return render_template('subscribe.html', shop=shop)

if __name__ == '__main__':
    app.run(debug=True)
