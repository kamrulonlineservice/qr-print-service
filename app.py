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

# হোম পেজ - প্রথম বা ডিফল্ট শপ দেখানোর জন্য (অথবা সরাসরি রেজিস্ট্রেশন পেজে রিডাইরেক্ট করতে পারেন)
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
    
    # একাধিক দোকান থাকলে তাদের নিজস্ব ইউনিক লিংকে যাওয়ার জন্য রিডাইরেক্ট করতে পারেন
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

        # নতুন শপ সেভ করা (স্বয়ংক্রয়ভাবে বর্তমান সময় এবং ট্রায়াল শুরু হবে)
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

        # রেজিস্ট্রেশন সফল হলে সরাসরি ওই দোকানের ইউনিক লিংকে পাঠিয়ে দেবে
        return redirect(url_for('view_shop', shop_id=new_shop.id))
    
    return render_template('register.html')

# সাবস্ক্রিপশন বা পেমেন্ট পেজ (৩ দিন পার হলে এখানে আসবে)
@app.route('/subscribe/<int:shop_id>', methods=['GET', 'POST'])
def subscribe(shop_id):
    shop = Shop.query.get_or_404(shop_id)
    
    if request.method == 'POST':
        # দোকানদার পেমেন্ট করার পর সাবমিট করলে আপনি চাইলে এখানে ট্রানজেকশন আইডি রাখতে পারেন
        # আপাতত নোটিফিকেশন বা ইনফো দেখাতে পারেন যে অ্যাডমিন অ্যাপ্রুভ করলে চালু হবে
        return "আপনার পেমেন্ট রিকোয়েস্ট জমা হয়েছে। খুব শীঘ্রই অ্যাকাউন্টটি একটিভ করে দেওয়া হবে।"
        
    return render_template('subscribe.html', shop=shop)

if __name__ == '__main__':
    app.run(debug=True)
