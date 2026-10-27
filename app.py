import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# ডেটাবেজ কনফিগারেশন
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///shops.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# শপ ডেটাবেজ মডেল (ট্রায়াল এবং পেমেন্ট স্ট্যাটাস সহ)
class Shop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_name = db.Column(db.String(150), nullable=False)
    owner_name = db.Column(db.String(100), nullable=False)
    address = db.Column(db.Text, nullable=False)
    qr_image_url = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow) # রেজিস্ট্রেশনের সময়
    is_paid = db.Column(db.Boolean, default=False) # পেমেন্ট স্ট্যাটাস

with app.app_context():
    db.create_all()

# হোম পেজ - প্রথম বা ডিফল্ট শপ দেখানোর জন্য
@app.route('/')
def index():
    shop = Shop.query.first()
    if not shop:
        shop_data = {
            'shop_name': 'KAMRUL ONLINE SERVICE',
            'address': 'Gaon Pradhan office vill: Garoimari p.o:garoimari dist: Morigaon pin-782104',
            'qr_image_url': 'https://raw.githubusercontent.com/kamrulonlineservice/qr-print-service/main/static/qr.png'
        }
        return render_template('index.html', shop=shop_data)
    
    return redirect(url_for('view_shop', shop_id=shop.id))

# নির্দিষ্ট দোকানদারের পেজ (৩ দিনের ট্রায়াল ও সাবস্ক্রিপশন চেক সহ)
@app.route('/shop/<int:shop_id>')
def view_shop(shop_id):
    shop = Shop.query.get_or_404(shop_id)
    
    # ৩ দিনের ট্রায়াল সময় হিসাব করা
    expiry_date = shop.created_at + timedelta(days=3)
    is_trial_active = datetime.utcnow() < expiry_date
    
    # যদি ৩ দিন পার হয়ে যায় এবং পেমেন্ট না করা থাকে, তবে সাবস্ক্রিপশন পেজে পাঠিয়ে দেবে
    if not is_trial_active and not shop.is_paid:
        return redirect(url_for('subscribe', shop_id=shop.id))
        
    shop_data = {
        'shop_name': shop.shop_name,
        'address': shop.address,
        'qr_image_url': shop.qr_image_url
    }
    return render_template('index.html', shop=shop_data)

# নতুন দোকান রেজিস্টার ফর্ম (৩ দিনের ফ্রি ট্রায়াল শুরু হবে)
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        shop_name = request.form.get('shop_name')
        owner_name = request.form.get('owner_name')
        address = request.form.get('address')
        qr_image_url = request.form.get('qr_image_url')

        new_shop = Shop(
            shop_name=shop_name, 
            owner_name=owner_name, 
            address=address, 
            qr_image_url=qr_image_url,
            created_at=datetime.utcnow(),
            is_paid=False
        )
        db.session.add(new_shop)
        db.session.commit()

        return redirect(url_for('view_shop', shop_id=new_shop.id))
    
    return render_template('register.html')

# সাবস্ক্রিপশন বা পেমেন্ট পেজ (৩ দিন পার হলে এখানে আসবে)
@app.route('/subscribe/<int:shop_id>', methods=['GET', 'POST'])
def subscribe(shop_id):
    shop = Shop.query.get_or_404(shop_id)
    
    if request.method == 'POST':
        return "আপনার পেমেন্ট রিকোয়েস্ট জমা হয়েছে। খুব শীঘ্রই অ্যাকাউন্টটি একটিভ করে দেওয়া হবে।"
        
    return render_template('subscribe.html', shop=shop)

# অ্যাডমিন লগইন পেজ (ইউজারনেম এবং পাসওয়ার্ড সহ)
@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # এখানে আপনার পছন্দমতো ইউজারনেম (যেমন: admin) এবং পাসওয়ার্ড (যেমন: kamrul123) সেট করতে পারেন
        if username == 'admin' and password == 'kamrul123':
            return redirect(url_for('admin_dashboard'))
        else:
            return "ভুল ইউজারনেম অথবা পাসওয়ার্ড! দয়া করে সঠিক তথ্য দিন।"
            
    return '''
        <form method="POST" style="max-width: 320px; margin: 100px auto; font-family: sans-serif; text-align: center; padding: 25px; border: 1px solid #cbd5e1; border-radius: 10px; background: #ffffff; box-shadow: 0 4px 15px rgba(0,0,0,0.05);">
            <h3 style="color: #1e293b; margin-bottom: 20px;">Admin Login</h3>
            <input type="text" name="username" placeholder="Admin Username" required style="width: 100%; padding: 10px; margin-bottom: 12px; border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box;"><br>
            <input type="password" name="password" placeholder="Admin Password" required style="width: 100%; padding: 10px; margin-bottom: 15px; border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box;"><br>
            <button type="submit" style="background: #2563eb; color: white; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; font-weight: bold; width: 100%;">Login</button>
        </form>
    '''

# অ্যাডমিন ড্যাশবোর্ড
@app.route('/admin/dashboard')
def admin_dashboard():
    shops = Shop.query.all()
    return render_template('admin.html', shops=shops)

# পেমেন্ট অ্যাপ্রুভ বা স্ট্যাটাস টগল করার রাউট
@app.route('/admin/approve/<int:shop_id>')
def approve_shop(shop_id):
    shop = Shop.query.get_or_404(shop_id)
    shop.is_paid = not shop.is_paid
    db.session.commit()
    return redirect(url_for('admin_dashboard'))

if __name__ == '__main__':
    app.run(debug=True)
