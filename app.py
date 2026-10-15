import os,io,secrets,string,urllib.parse
from datetime import datetime, timedelta
import qrcode,requests
from flask import Flask,render_template,request,redirect,url_for,flash,abort,send_file,session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager,UserMixin,login_user,logout_user,login_required,current_user
from werkzeug.security import generate_password_hash,check_password_hash
# Redeploy trigger: apply current Render environment settings.
app=Flask(__name__); app.config["SECRET_KEY"]=os.getenv("SECRET_KEY","dev-only-change-me")
dburl=os.getenv("DATABASE_URL","sqlite:///flankvpn.db")
# Render PostgreSQL URLs use psycopg2, which is installed via psycopg2-binary.
if dburl.startswith("postgres://"):
 dburl=dburl.replace("postgres://","postgresql+psycopg2://",1)
elif dburl.startswith("postgresql://"):
 dburl=dburl.replace("postgresql://","postgresql+psycopg2://",1)
app.config["SQLALCHEMY_DATABASE_URI"]=dburl; app.config["SQLALCHEMY_TRACK_MODIFICATIONS"]=False
app.config["PAYMENT_RECIPIENT"] = os.getenv("PAYMENT_RECIPIENT", "")
db=SQLAlchemy(app); lm=LoginManager(app); lm.login_view="login"
TARIFFS={"1":{"months":1,"price":199,"title":"1 месяц"},"3":{"months":3,"price":499,"title":"3 месяца"},"12":{"months":12,"price":1499,"title":"12 месяцев"}}
PAYMENTS={"sbp":{"title":"СБП","detail":os.getenv("PAYMENT_SBP_PHONE","Настройте PAYMENT_SBP_PHONE")},"card":{"title":"Банковская карта","detail":os.getenv("PAYMENT_CARD_NUMBER","Настройте PAYMENT_CARD_NUMBER")}}
class User(UserMixin,db.Model):
 id=db.Column(db.Integer,primary_key=True); nickname=db.Column(db.String(40),unique=True,nullable=False); email=db.Column(db.String(160),unique=True,nullable=False); password_hash=db.Column(db.String(255),nullable=False)
 def set_password(self,p): self.password_hash=generate_password_hash(p)
 def check_password(self,p): return check_password_hash(self.password_hash,p)
class Order(db.Model):
 id=db.Column(db.Integer,primary_key=True); user_id=db.Column(db.Integer,db.ForeignKey("user.id"),nullable=False); nickname=db.Column(db.String(40),nullable=False); email=db.Column(db.String(160),nullable=False); tariff_key=db.Column(db.String(8),nullable=False); amount=db.Column(db.Integer,nullable=False); payment_method=db.Column(db.String(12),nullable=False); status=db.Column(db.String(24),default="awaiting_payment"); created_at=db.Column(db.DateTime,default=datetime.utcnow); subscription_url=db.Column(db.Text); error_message=db.Column(db.Text)
 user=db.relationship("User",backref=db.backref("orders",lazy=True))
class EmailVerification(db.Model):
 id=db.Column(db.Integer,primary_key=True)
 email=db.Column(db.String(160),nullable=False,index=True)
 nickname=db.Column(db.String(40),nullable=False)
 password_hash=db.Column(db.String(255),nullable=False)
 code_hash=db.Column(db.String(255),nullable=False)
 expires_at=db.Column(db.DateTime,nullable=False)
 attempts=db.Column(db.Integer,default=0)
 created_at=db.Column(db.DateTime,default=datetime.utcnow)
@lm.user_loader
def load_user(uid): return db.session.get(User,int(uid))
def is_admin(): return bool(session.get("flankadmin"))
@app.context_processor
def globals_(): return {"tariffs":TARIFFS,"payments":PAYMENTS}
@app.route("/")
def index(): return render_template("index.html")
@app.route("/register",methods=["GET","POST"])
def register():
 if request.method=="POST":
  nick=request.form.get("nickname","").strip(); email=request.form.get("email","").strip().lower(); pw=request.form.get("password","")
  if len(nick)<3 or len(nick)>40 or "@" not in email or len(pw)<8:
   flash("Проверь ник, email и пароль (не менее 8 символов).","error")
  elif User.query.filter((User.nickname==nick)|(User.email==email)).first():
   flash("Этот ник или email уже занят.","error")
  elif not os.getenv("RESEND_API_KEY") or not os.getenv("RESEND_FROM_EMAIL"):
   flash("Регистрация временно недоступна: администратор не настроил отправку email-кодов.","error")
  else:
   code="".join(secrets.choice(string.digits) for _ in range(6))
   pending=EmailVerification.query.filter_by(email=email).order_by(EmailVerification.created_at.desc()).first()
   if pending and pending.created_at > datetime.utcnow()-timedelta(seconds=60):
    flash("Подожди минуту перед повторной отправкой кода.","error")
    return render_template("auth.html",mode="register")
   try:
    resp=requests.post("https://api.resend.com/emails",
      headers={"Authorization":"Bearer "+os.getenv("RESEND_API_KEY"),"Content-Type":"application/json"},
      json={"from":os.getenv("RESEND_FROM_EMAIL"),"to":[email],"subject":"Код подтверждения FlankVPN",
            "html":f"<div style='font-family:Arial,sans-serif'><h2>FlankVPN</h2><p>Твой код подтверждения:</p><p style='font-size:32px;font-weight:bold;letter-spacing:8px'>{code}</p><p>Код действует 10 минут. Если ты не регистрировался, проигнорируй письмо.</p></div>"},
      timeout=15)
    resp.raise_for_status()
    if pending:
     pending.nickname=nick; pending.password_hash=generate_password_hash(pw); pending.code_hash=generate_password_hash(code)
     pending.expires_at=datetime.utcnow()+timedelta(minutes=10); pending.attempts=0; pending.created_at=datetime.utcnow()
    else:
     pending=EmailVerification(email=email,nickname=nick,password_hash=generate_password_hash(pw),
       code_hash=generate_password_hash(code),expires_at=datetime.utcnow()+timedelta(minutes=10))
     db.session.add(pending)
    db.session.commit()
    session["verify_email"]=email
    flash("Код отправлен на email. Проверь также папку «Спам».","success")
    return redirect(url_for("verify_email"))
   except Exception:
    app.logger.exception("Could not send registration verification email")
    flash("Не удалось отправить письмо. Проверь настройки Resend и попробуй позже.","error")
 return render_template("auth.html",mode="register")

@app.route("/verify-email",methods=["GET","POST"])
def verify_email():
 email=session.get("verify_email")
 if not email: return redirect(url_for("register"))
 pending=EmailVerification.query.filter_by(email=email).order_by(EmailVerification.created_at.desc()).first()
 if not pending: session.pop("verify_email",None); return redirect(url_for("register"))
 if request.method=="POST":
  code=request.form.get("code","").strip()
  if datetime.utcnow()>pending.expires_at:
   db.session.delete(pending); db.session.commit(); session.pop("verify_email",None)
   flash("Код истёк. Зарегистрируйся ещё раз, чтобы получить новый.","error")
   return redirect(url_for("register"))
  if pending.attempts>=5:
   flash("Слишком много попыток. Начни регистрацию заново.","error")
   return redirect(url_for("register"))
  pending.attempts+=1
  if check_password_hash(pending.code_hash,code):
   if User.query.filter((User.nickname==pending.nickname)|(User.email==pending.email)).first():
    db.session.delete(pending); db.session.commit(); session.pop("verify_email",None)
    flash("Этот ник или email уже зарегистрирован. Войди в аккаунт.","error")
    return redirect(url_for("login"))
   user=User(nickname=pending.nickname,email=pending.email,password_hash=pending.password_hash)
   db.session.add(user); db.session.delete(pending); db.session.commit(); session.pop("verify_email",None); login_user(user)
   flash("Email подтверждён. Аккаунт создан!","success")
   return redirect(url_for("dashboard"))
  db.session.commit(); flash("Неверный код. Проверь письмо и попробуй ещё раз.","error")
 return render_template("verify.html",email=email)
@app.route("/login",methods=["GET","POST"])
def login():
 if request.method=="POST":
  ident=request.form.get("identity","").strip(); u=User.query.filter((User.email==ident.lower())|(User.nickname==ident)).first()
  if u and u.check_password(request.form.get("password","")): login_user(u); return redirect(url_for("dashboard"))
  flash("Неверный ник/email или пароль.","error")
 return render_template("auth.html",mode="login")
@app.route("/logout",methods=["POST"])
@login_required
def logout():
 logout_user(); session.pop("flankadmin",None); return redirect(url_for("index"))
@app.route("/dashboard")
@login_required
def dashboard(): return render_template("dashboard.html",orders=Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all())
@app.route("/checkout",methods=["POST"])
@login_required
def checkout():
 k=request.form.get("tariff"); m=request.form.get("method")
 if k not in TARIFFS or m not in PAYMENTS: flash("Выберите тариф и способ оплаты.","error"); return redirect(url_for("dashboard"))
 t=TARIFFS[k]; o=Order(user_id=current_user.id,nickname=current_user.nickname,email=current_user.email,tariff_key=k,amount=t["price"],payment_method=m); db.session.add(o); db.session.commit(); return redirect(url_for("payment",oid=o.id))
@app.route("/payment/<int:oid>")
@login_required
def payment(oid):
 o=db.session.get(Order,oid)
 if not o or o.user_id!=current_user.id: abort(404)
 return render_template("payment.html",order=o,payment=PAYMENTS[o.payment_method],tariff=TARIFFS[o.tariff_key])
@app.route("/payment/<int:oid>/paid",methods=["POST"])
@login_required
def paid(oid):
 o=db.session.get(Order,oid)
 if not o or o.user_id!=current_user.id: abort(404)
 if o.status=="awaiting_payment": o.status="awaiting_review"; db.session.commit(); flash("Заявка отправлена. Администратор проверит поступление денег.","success")
 return redirect(url_for("dashboard"))
@app.route("/subscription-qr/<int:oid>.png")
@login_required
def qr(oid):
 o=db.session.get(Order,oid)
 if not o or o.user_id!=current_user.id or not o.subscription_url: abort(404)
 im=qrcode.make(o.subscription_url); out=io.BytesIO(); im.save(out,format="PNG"); out.seek(0); return send_file(out,mimetype="image/png")
@app.route("/admin/login",methods=["GET","POST"])
def admin_login():
 if request.method=="POST" and request.form.get("username")==os.getenv("ADMIN_USERNAME","admin") and request.form.get("password")==os.getenv("ADMIN_PASSWORD","change-this-now"):
  session["flankadmin"]=True; return redirect(url_for("admin_orders"))
 elif request.method=="POST": flash("Неверные данные администратора.","error")
 return render_template("admin_login.html")
@app.route("/admin")
@app.route("/admin/orders")
def admin_orders():
 if not is_admin(): return redirect(url_for("admin_login"))
 orders=Order.query.order_by(Order.created_at.desc()).all(); return render_template("admin.html",orders=orders,total_pending=sum(o.amount for o in orders if o.status=="awaiting_review"))
def provision(o):
 base=os.getenv("H1_API_BASE","").rstrip("/"); token=os.getenv("H1_API_TOKEN","")
 if not base or not token: raise RuntimeError("Настройте H1_API_BASE и H1_API_TOKEN в переменных окружения.")
 name="flank_"+str(o.id)+"_"+''.join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(5)); h={"Authorization":"Bearer "+token,"Content-Type":"application/json"}
 r=requests.post(base+"/create",headers=h,json={"name":name,"days":TARIFFS[o.tariff_key]["months"]*30,"traffic_limit_gb":0,"device_limit":0},timeout=25); r.raise_for_status(); d=r.json()
 if d.get("ok") is False: raise RuntimeError(str(d))
 r=requests.get(base+"/info",headers=h,params={"name":name},timeout=25); r.raise_for_status(); c=r.json().get("client",r.json()); sub=c.get("sub_url") or c.get("subscription_url")
 if not sub and c.get("uuid") and os.getenv("H1_SUBSCRIPTION_BASE"): sub=urllib.parse.urljoin(os.getenv("H1_SUBSCRIPTION_BASE").rstrip("/")+"/",c["uuid"])
 if not sub: raise RuntimeError("H1 не вернула sub_url для нового клиента.")
 o.subscription_url=sub+("&" if "?" in sub else "?")+"format=json"; o.status="active"; o.error_message=None; db.session.commit()
@app.route("/admin/confirm/<int:oid>",methods=["POST"])
def confirm(oid):
 if not is_admin(): abort(403)
 o=db.session.get(Order,oid)
 if not o: abort(404)
 if o.status not in ("awaiting_review","error"): flash("Нельзя подтвердить заказ в этом статусе.","error"); return redirect(url_for("admin_orders"))
 try: o.status="processing"; db.session.commit(); provision(o); flash("Доступ выдан для "+o.nickname+".","success")
 except Exception as e: db.session.rollback(); o=db.session.get(Order,oid); o.status="error"; o.error_message=str(e)[:400]; db.session.commit(); app.logger.exception("H1 API error")
 return redirect(url_for("admin_orders"))
@app.route("/admin/reject/<int:oid>",methods=["POST"])
def reject(oid):
 if not is_admin(): abort(403)
 o=db.session.get(Order,oid)
 if o and o.status=="awaiting_review": o.status="rejected"; db.session.commit()
 return redirect(url_for("admin_orders"))
with app.app_context(): db.create_all()
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.getenv("PORT","5001")),debug=False)
