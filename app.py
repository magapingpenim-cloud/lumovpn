import os,io,secrets,string,urllib.parse
from datetime import datetime, timedelta
import qrcode,requests
from flask import Flask,render_template,request,redirect,url_for,flash,abort,send_file,session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager,UserMixin,login_user,logout_user,login_required,current_user
from werkzeug.security import generate_password_hash,check_password_hash
from sqlalchemy import func, inspect, text
from html import escape
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
TARIFFS={"trial":{"months":0,"days":10,"price":0,"title":"Бесплатный пробный период — 10 дней"},"game":{"months":0,"days":1,"price":0,"title":"Обмен монет кликера на VPN"},"1":{"months":1,"price":49,"title":"1 месяц","discount":0,"save":0},"3":{"months":3,"price":132,"title":"3 месяца","discount":10,"save":15},"12":{"months":12,"price":470,"title":"12 месяцев","discount":20,"save":118}}
PAYMENTS={"sbp":{"title":"СБП","detail":os.getenv("PAYMENT_SBP_PHONE","Настройте PAYMENT_SBP_PHONE")},"card":{"title":"Банковская карта","detail":os.getenv("PAYMENT_CARD_NUMBER","Настройте PAYMENT_CARD_NUMBER")}}

def notify_admin_telegram(message):
 token=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
 chat_id=os.getenv("TELEGRAM_CHAT_ID","").strip()
 if not token or not chat_id:
  app.logger.info("Telegram notifications are disabled: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
  return False
 try:
  response=requests.post("https://api.telegram.org/bot"+token+"/sendMessage",
   json={"chat_id":chat_id,"text":message,"disable_web_page_preview":True},timeout=10)
  response.raise_for_status()
  data=response.json()
  if not data.get("ok"): raise RuntimeError("Telegram Bot API did not confirm the message.")
  return True
 except Exception:
  app.logger.exception("Telegram admin notification failed")
  return False

def send_subscription_email(order):
 api_key=os.getenv("RESEND_API_KEY","").strip()
 sender=os.getenv("RESEND_FROM_EMAIL","").strip()
 sub_url=(order.subscription_url or "").strip()
 if not api_key or not sender or not order.email or not sub_url:
  app.logger.error("Subscription email not sent: missing Resend setting, email, or subscription URL for order %s.",order.id)
  return False
 safe_nick=escape(order.nickname or "")
 safe_url=escape(sub_url,quote=True)
 subject="Твой бесплатный пробный период FlankVPN готов" if order.tariff_key=="trial" else "Твоя подписка FlankVPN готова"
 trial_note="<p><strong>Пробный период:</strong> 10 дней бесплатно с момента подключения.</p>" if order.tariff_key=="trial" else ""
 html_body=f"""
 <div style="margin:0;background:#f2f6fc;padding:28px 12px;font-family:Arial,Helvetica,sans-serif;color:#10203a">
  <div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #dce7f5;border-radius:20px;overflow:hidden">
   <div style="padding:26px 28px;background:#081529;color:#fff">
    <div style="font-size:12px;letter-spacing:3px;color:#8fe1ff;font-weight:bold">FLANKVPN</div>
    <h1 style="font-size:25px;margin:12px 0 6px">Твоя подписка готова!</h1>
    <p style="margin:0;color:#d4e3f7">Заказ #{order.id}</p>
   </div>
   <div style="padding:28px">
    <p>Привет, <strong>{safe_nick}</strong>! VPN-доступ создан.</p>
    {trial_note}
    <p>Нажми кнопку ниже, чтобы открыть ссылку подписки и импортировать её в совместимый VPN-клиент.</p>
    <p style="text-align:center;margin:28px 0">
     <a href="{safe_url}" style="display:inline-block;padding:14px 22px;border-radius:12px;color:#fff;background:#1769ff;text-decoration:none;font-weight:bold">Открыть подписку</a>
    </p>
    <p style="font-size:12px;color:#5c6d85">Если кнопка не открывается, скопируй эту ссылку:</p>
    <p style="font-size:12px;line-height:1.6;word-break:break-all"><a href="{safe_url}">{safe_url}</a></p>
    <p style="font-size:12px;color:#718099;margin-top:24px">Если ты не оформлял заказ, можешь проигнорировать это письмо.</p>
   </div>
  </div>
 </div>
 """
 text_body=(f"FlankVPN: бесплатный пробный период на 10 дней. Заказ #{order.id}. " if order.tariff_key=="trial" else f"FlankVPN: подписка готова. Заказ #{order.id}. ") + f"Никнейм: {order.nickname}. Ссылка подписки: {sub_url}"
 try:
  response=requests.post("https://api.resend.com/emails",
   headers={"Authorization":"Bearer "+api_key,"Content-Type":"application/json",
    "Idempotency-Key":f"flankvpn-subscription-order-{order.id}"},
   json={"from":sender,"to":[order.email],"subject":subject,"html":html_body,"text":text_body},
   timeout=15)
  response.raise_for_status()
  return True
 except Exception:
  app.logger.exception("Failed to email subscription for order %s to %s.",order.id,order.email)
  return False
class User(UserMixin,db.Model):
 id=db.Column(db.Integer,primary_key=True); nickname=db.Column(db.String(40),unique=True,nullable=False); email=db.Column(db.String(160),unique=True,nullable=False); password_hash=db.Column(db.String(255),nullable=False)
 def set_password(self,p): self.password_hash=generate_password_hash(p)
 def check_password(self,p): return check_password_hash(self.password_hash,p)
class Order(db.Model):
 id=db.Column(db.Integer,primary_key=True); user_id=db.Column(db.Integer,db.ForeignKey("user.id"),nullable=False); nickname=db.Column(db.String(40),nullable=False); email=db.Column(db.String(160),nullable=False); tariff_key=db.Column(db.String(8),nullable=False); amount=db.Column(db.Integer,nullable=False); payment_method=db.Column(db.String(12),nullable=False); status=db.Column(db.String(24),default="awaiting_payment"); created_at=db.Column(db.DateTime,default=datetime.utcnow); subscription_url=db.Column(db.Text); error_message=db.Column(db.Text); requested_days=db.Column(db.Integer)
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


class GameState(db.Model):
 id=db.Column(db.Integer,primary_key=True)
 user_id=db.Column(db.Integer,db.ForeignKey("user.id"),unique=True,nullable=False)
 coins=db.Column(db.Integer,default=0,nullable=False)
 click_power=db.Column(db.Integer,default=1,nullable=False)
 income_per_second=db.Column(db.Integer,default=0,nullable=False)
 autoclicker=db.Column(db.Boolean,default=False,nullable=False)
 contest_points=db.Column(db.Integer,default=0,nullable=False)
 redeem_count=db.Column(db.Integer,default=0,nullable=False)
 last_tick=db.Column(db.DateTime,default=datetime.utcnow,nullable=False)
 user=db.relationship("User",backref=db.backref("game_state",uselist=False))

class GamePurchase(db.Model):
 id=db.Column(db.Integer,primary_key=True)
 user_id=db.Column(db.Integer,db.ForeignKey("user.id"),nullable=False)
 nickname=db.Column(db.String(40),nullable=False)
 email=db.Column(db.String(160),nullable=False)
 item_key=db.Column(db.String(40),nullable=False)
 amount=db.Column(db.Integer,nullable=False,default=100)
 payment_method=db.Column(db.String(12),nullable=False)
 status=db.Column(db.String(24),nullable=False,default="awaiting_payment")
 created_at=db.Column(db.DateTime,default=datetime.utcnow)
 user=db.relationship("User",backref=db.backref("game_purchases",lazy=True))


class SupportMessage(db.Model):
 id=db.Column(db.Integer,primary_key=True)
 user_id=db.Column(db.Integer,db.ForeignKey("user.id"),nullable=True)
 nickname=db.Column(db.String(80),nullable=False)
 email=db.Column(db.String(160),nullable=False,default="")
 message=db.Column(db.Text,nullable=False)
 status=db.Column(db.String(20),nullable=False,default="new")
 created_at=db.Column(db.DateTime,default=datetime.utcnow,nullable=False)
 user=db.relationship("User",backref=db.backref("support_messages",lazy=True))


class GameCampaign(db.Model):
 id=db.Column(db.Integer,primary_key=True)
 start_at=db.Column(db.DateTime,nullable=False)
 end_at=db.Column(db.DateTime,nullable=False)
 winner_user_id=db.Column(db.Integer,db.ForeignKey("user.id"),nullable=True)
 winner_user=db.relationship("User")
 prize_order_id=db.Column(db.Integer,nullable=True)

def get_game_campaign():
 campaign=db.session.get(GameCampaign,1)
 if not campaign:
  now=datetime.utcnow()
  campaign=GameCampaign(id=1,start_at=now,end_at=now+timedelta(days=30))
  db.session.add(campaign)
  try:
   db.session.commit()
  except Exception:
   db.session.rollback()
   campaign=db.session.get(GameCampaign,1)
 return campaign

GAME_UPGRADES={"power":{"title":"Сильнее удар","base":35,"description":"+1 монета за каждый клик"},"income":{"title":"Пассивный доход","base":80,"description":"+1 монета в секунду"}}
COINS_PER_VPN_DAY=10000

def game_state_for(user):
 state=GameState.query.filter_by(user_id=user.id).first()
 if not state:
  state=GameState(user_id=user.id); db.session.add(state); db.session.commit()
 now=datetime.utcnow()
 campaign=get_game_campaign()
 previous_tick=state.last_tick
 elapsed=max(0,min(int((now-previous_tick).total_seconds()),43200))
 if elapsed:
  rate=state.income_per_second + (state.click_power if state.autoclicker else 0)
  state.coins += elapsed*rate
  contest_start=max(previous_tick,campaign.start_at)
  contest_end=min(now,campaign.end_at)
  contest_seconds=max(0,int((contest_end-contest_start).total_seconds()))
  if contest_seconds:
   state.contest_points += contest_seconds*rate
  state.last_tick=now
  db.session.commit()
 return state

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
  nick=request.form.get("nickname","").strip().lower(); email=request.form.get("email","").strip().lower(); pw=request.form.get("password","")
  if len(nick)<3 or len(nick)>40 or "@" not in email or len(pw)<8:
   flash("Проверь ник, email и пароль (не менее 8 символов).","error")
  elif User.query.filter((func.lower(User.nickname)==nick)|(func.lower(User.email)==email)).first():
   flash("Этот ник или email уже занят. Никнейм не должен повторяться; регистр букв не учитывается.","error")
  elif EmailVerification.query.filter(func.lower(EmailVerification.nickname)==nick, EmailVerification.email!=email, EmailVerification.expires_at>datetime.utcnow()).first():
   flash("Этот никнейм уже используется в другой незавершённой регистрации. Выбери другой ник.","error")
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
   if User.query.filter((func.lower(User.nickname)==pending.nickname.lower())|(func.lower(User.email)==pending.email.lower())).first():
    db.session.delete(pending); db.session.commit(); session.pop("verify_email",None)
    flash("Этот ник или email уже зарегистрирован. Войди в аккаунт.","error")
    return redirect(url_for("login"))
   user=User(nickname=pending.nickname,email=pending.email,password_hash=pending.password_hash)
   db.session.add(user); db.session.flush()
   trial=Order(user_id=user.id,nickname=user.nickname,email=user.email,tariff_key="trial",amount=0,payment_method="trial",status="processing")
   db.session.add(trial); db.session.delete(pending); db.session.commit(); session.pop("verify_email",None); login_user(user)
   try:
    provision(trial)
    email_sent=send_subscription_email(trial)
    notify_admin_telegram(f"🆓 Бесплатный пробный период выдан\nНикнейм: {trial.nickname}\nEmail: {trial.email}\nСрок: 10 дней\nПисьмо со ссылкой: {'отправлено' if email_sent else 'не отправлено — проверь Resend'}")
    if email_sent:
     flash("Аккаунт создан! Твои первые 10 дней FlankVPN бесплатны. Ссылка также отправлена на email.","success")
    else:
     flash("Аккаунт создан, а пробный доступ подготовлен. Ссылка видна в личном кабинете; проверь почту и настройки отправки email.","success")
   except Exception as e:
    db.session.rollback(); trial=db.session.get(Order,trial.id); trial.status="error"; trial.error_message=str(e)[:400]; db.session.commit()
    app.logger.exception("Could not provision free FlankVPN trial")
    notify_admin_telegram(f"⚠️ Не удалось автоматически выдать пробный период\nНикнейм: {trial.nickname}\nEmail: {trial.email}\nПричина: {trial.error_message}")
    flash("Аккаунт создан, но автоматическая выдача пробного доступа не сработала. Администратор получил уведомление и сможет повторить выдачу.","error")
   return redirect(url_for("dashboard"))
  db.session.commit(); flash("Неверный код. Проверь письмо и попробуй ещё раз.","error")
 return render_template("verify.html",email=email)
@app.route("/login",methods=["GET","POST"])
def login():
 if request.method=="POST":
  ident=request.form.get("identity","").strip(); u=User.query.filter((func.lower(User.email)==ident.lower())|(func.lower(User.nickname)==ident.lower())).first()
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
 if k not in TARIFFS or k in ("trial","game") or m not in PAYMENTS: flash("Выберите платный тариф и способ оплаты.","error"); return redirect(url_for("dashboard"))
 t=TARIFFS[k]; o=Order(user_id=current_user.id,nickname=current_user.nickname,email=current_user.email,tariff_key=k,amount=t["price"],payment_method=m); db.session.add(o); db.session.commit()
 notify_admin_telegram(f"🆕 Новый заказ FlankVPN #{o.id}\nНикнейм: {o.nickname}\nEmail: {o.email}\nТариф: {t['title']}\nСумма: {o.amount} ₽\nСпособ: {PAYMENTS[m]['title']}\nСтатус: ожидает оплаты")
 return redirect(url_for("payment",oid=o.id))
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
 if o.status=="awaiting_payment":
  o.status="awaiting_review"; db.session.commit()
  notify_admin_telegram(f"💳 Клиент заявил об оплате заказа #{o.id}\nНикнейм: {o.nickname}\nEmail: {o.email}\nСумма: {o.amount} ₽\nВажно: это заявка клиента; поступление денег нужно проверить вручную.")
  flash("Заявка отправлена. Администратор проверит поступление денег.","success")
 return redirect(url_for("dashboard"))
@app.route("/subscription-qr/<int:oid>.png")
@login_required
def qr(oid):
 o=db.session.get(Order,oid)
 if not o or o.user_id!=current_user.id or not o.subscription_url: abort(404)
 im=qrcode.make(o.subscription_url); out=io.BytesIO(); im.save(out,format="PNG"); out.seek(0); return send_file(out,mimetype="image/png")


@app.route("/game")
@login_required
def game():
 state=game_state_for(current_user)
 purchases=GamePurchase.query.filter_by(user_id=current_user.id).order_by(GamePurchase.created_at.desc()).limit(10).all()
 upgrades={k:{**v,"price":v["base"]*(2**(state.click_power-1) if k=="power" else 2**state.income_per_second)} for k,v in GAME_UPGRADES.items()}
 income_rate=state.income_per_second + (state.click_power if state.autoclicker else 0)
 campaign=get_game_campaign()
 leaderboard=User.query.join(GameState,GameState.user_id==User.id).order_by(GameState.contest_points.desc(),User.nickname.asc()).limit(10).all()
 return render_template("game.html",state=state,upgrades=upgrades,purchases=purchases,payments=PAYMENTS,income_rate=income_rate,campaign=campaign,leaderboard=leaderboard,now=datetime.utcnow(),coins_per_vpn_day=COINS_PER_VPN_DAY,next_vpn_day_price=COINS_PER_VPN_DAY+2000*state.redeem_count,redeem_count=state.redeem_count)

@app.route("/game/tap",methods=["POST"])
@login_required
def game_tap():
 state=game_state_for(current_user)
 gained=state.click_power
 state.coins += gained
 campaign=get_game_campaign()
 now=datetime.utcnow()
 if campaign.start_at <= now < campaign.end_at:
  state.contest_points += gained
 db.session.commit()
 return {"coins":state.coins,"click_power":state.click_power,"income":state.income_per_second + (state.click_power if state.autoclicker else 0),"contest_points":state.contest_points}

@app.route("/game/redeem",methods=["POST"])
@login_required
def game_redeem():
 state=game_state_for(current_user)
 try:
  days=int(request.form.get("days","1"))
 except (TypeError,ValueError):
  days=0
 if days<1 or days>365:
  flash("Укажи количество суток от 1 до 365.","error")
  return redirect(url_for("game"))
 # Progressive pricing: 1st day 10,000 coins, 2nd 20,000, 3rd 30,000, etc.
 unit_price=COINS_PER_VPN_DAY+2000*state.redeem_count
 cost=unit_price*days
 if state.coins<cost:
  flash(f"Для {days} суток нужно {cost:,} монет, у тебя {state.coins:,}.".replace(",", " "),"error")
  return redirect(url_for("game"))
 pending=Order.query.filter_by(user_id=current_user.id,tariff_key="game",status="processing").first()
 if pending:
  flash("У тебя уже обрабатывается обмен VPN-времени. Обнови страницу через минуту.","error")
  return redirect(url_for("game"))
 order=Order(user_id=current_user.id,nickname=current_user.nickname,email=current_user.email,tariff_key="game",amount=0,payment_method="game",status="processing",requested_days=days)
 db.session.add(order)
 state.coins=0
 state.click_power=1
 state.income_per_second=0
 state.last_tick=datetime.utcnow()
 db.session.commit()
 try:
  provision(order)
  state.redeem_count += 1
  db.session.commit()
  email_sent=send_subscription_email(order)
  flash(f"Обмен выполнен: {days} сутки VPN. Монеты, сила тапа и пассивный доход сброшены; автокликер сохранён." + ( " Ссылка отправлена на email." if email_sent else " Ссылка доступна в личном кабинете; письмо не удалось отправить."),"success")
 except Exception as e:
  db.session.rollback()
  order=db.session.get(Order,order.id)
  if order:
   order.status="error"; order.error_message=str(e)[:400]; db.session.commit()
  app.logger.exception("Game VPN redemption failed")
  notify_admin_telegram(f"⚠️ Ошибка обмена монет на VPN для {current_user.nickname}. Заказ #{order.id if order else 'unknown'}. Проверь панель администратора.")
  flash("Не удалось выдать VPN-время автоматически. Обращение записано; администратор сможет повторить выдачу.","error")
 return redirect(url_for("game"))

@app.route("/game/upgrade/<kind>",methods=["POST"])
@login_required
def game_upgrade(kind):
 if kind not in GAME_UPGRADES: abort(404)
 state=game_state_for(current_user)
 price=GAME_UPGRADES[kind]["base"]*(2**(state.click_power-1) if kind=="power" else 2**state.income_per_second)
 if state.coins<price:
  flash("Не хватает игровых монет для улучшения.","error")
 else:
  state.coins-=price
  if kind=="power": state.click_power+=1
  else: state.income_per_second+=1
  db.session.commit(); flash("Улучшение куплено!","success")
 return redirect(url_for("game"))

@app.route("/game/buy/autoclicker",methods=["POST"])
@login_required
def game_buy_autoclicker():
 state=game_state_for(current_user)
 if state.autoclicker:
  flash("Автокликер уже активирован.","success"); return redirect(url_for("game"))
 method=request.form.get("method","")
 if method not in PAYMENTS: abort(400)
 existing=GamePurchase.query.filter_by(user_id=current_user.id,item_key="autoclicker",status="awaiting_payment").first()
 if existing:
  flash("У тебя уже есть незавершённая заявка. Продолжи оплату ниже.","error"); return redirect(url_for("game_payment",pid=existing.id))
 purchase=GamePurchase(user_id=current_user.id,nickname=current_user.nickname,email=current_user.email,item_key="autoclicker",amount=100,payment_method=method)
 db.session.add(purchase); db.session.commit()
 notify_admin_telegram(f"🕹️ Заявка на автокликер FlankVPN #{purchase.id}\nНикнейм: {purchase.nickname}\nEmail: {purchase.email}\nСумма: 100 ₽\nСпособ: {PAYMENTS[method]['title']}\nОжидает оплаты")
 return redirect(url_for("game_payment",pid=purchase.id))

@app.route("/game/payment/<int:pid>")
@login_required
def game_payment(pid):
 purchase=db.session.get(GamePurchase,pid)
 if not purchase or purchase.user_id!=current_user.id: abort(404)
 return render_template("game_payment.html",purchase=purchase,payment=PAYMENTS[purchase.payment_method])

@app.route("/game/payment/<int:pid>/paid",methods=["POST"])
@login_required
def game_paid(pid):
 purchase=db.session.get(GamePurchase,pid)
 if not purchase or purchase.user_id!=current_user.id: abort(404)
 if purchase.status=="awaiting_payment":
  purchase.status="awaiting_review"; db.session.commit()
  notify_admin_telegram(f"💳 Заявка на автокликер #{purchase.id}: клиент сообщил об оплате\nНикнейм: {purchase.nickname}\nСумма: {purchase.amount} ₽\nСпособ: {PAYMENTS[purchase.payment_method]['title']}\nПоступление нужно проверить вручную.")
  flash("Заявка отправлена администратору. Автокликер включится после проверки оплаты.","success")
 return redirect(url_for("game"))

@app.route("/admin/game-purchase/<int:pid>/confirm",methods=["POST"])
def admin_confirm_game_purchase(pid):
 if not is_admin(): abort(403)
 purchase=db.session.get(GamePurchase,pid)
 if not purchase: abort(404)
 if purchase.status=="awaiting_review":
  state=game_state_for(purchase.user); state.autoclicker=True; purchase.status="active"; db.session.commit()
  notify_admin_telegram(f"✅ Автокликер активирован для {purchase.nickname}, заявка #{purchase.id}.")
 flash("Автокликер активирован." if purchase.status=="active" else "Сначала клиент должен сообщить об оплате.","success" if purchase.status=="active" else "error")
 return redirect(url_for("admin_orders"))

@app.route("/admin/game-purchase/<int:pid>/reject",methods=["POST"])
def admin_reject_game_purchase(pid):
 if not is_admin(): abort(403)
 purchase=db.session.get(GamePurchase,pid)
 if purchase and purchase.status in ("awaiting_payment","awaiting_review"):
  purchase.status="rejected"; db.session.commit()
 return redirect(url_for("admin_orders"))


@app.route("/support/send",methods=["POST"])
def support_send():
 message=request.form.get("message","").strip()
 if current_user.is_authenticated:
  nickname=current_user.nickname
  email=current_user.email
  user_id=current_user.id
 else:
  nickname=request.form.get("nickname","").strip()[:80]
  email=request.form.get("email","").strip()[:160]
  user_id=None
 if len(message)<3 or len(message)>2000:
  flash("Сообщение должно содержать от 3 до 2000 символов.","error")
  return redirect(request.referrer or url_for("index"))
 if not current_user.is_authenticated and (len(nickname)<2 or "@" not in email):
  flash("Укажи имя и корректный email, чтобы поддержка могла ответить.","error")
  return redirect(request.referrer or url_for("index"))
 item=SupportMessage(user_id=user_id,nickname=nickname,email=email,message=message)
 db.session.add(item)
 try:
  db.session.commit()
 except Exception:
  db.session.rollback()
  app.logger.exception("Could not save support message")
  flash("Не удалось отправить сообщение. Попробуй ещё раз.","error")
  return redirect(request.referrer or url_for("index"))
 notify_admin_telegram(f"✉️ Новое сообщение в поддержку FlankVPN #{item.id}\nОт: {item.nickname}\nEmail: {item.email or 'не указан'}\nСообщение: {item.message[:1200]}")
 flash("Сообщение отправлено в поддержку. Мы увидим его в панели администратора.","success")
 return redirect(request.referrer or url_for("index"))

@app.route("/admin/support/<int:mid>/close",methods=["POST"])
def admin_close_support(mid):
 if not is_admin(): abort(403)
 item=db.session.get(SupportMessage,mid)
 if not item: abort(404)
 item.status="closed"
 db.session.commit()
 flash("Обращение отмечено как обработанное.","success")
 return redirect(url_for("admin_orders"))

@app.route("/admin/game-contest/award",methods=["POST"])
def admin_award_game_contest():
 if not is_admin(): abort(403)
 campaign=get_game_campaign()
 now=datetime.utcnow()
 if now < campaign.end_at:
  flash("Акция ещё продолжается. Победителя можно назначить после завершения таймера.","error")
  return redirect(url_for("admin_orders"))
 if campaign.winner_user_id or campaign.prize_order_id:
  flash("Победитель акции уже назначен.","error")
  return redirect(url_for("admin_orders"))
 winner=User.query.join(GameState,GameState.user_id==User.id).order_by(GameState.contest_points.desc(),User.nickname.asc()).first()
 if not winner:
  flash("Нет участников — годовую подписку пока некому выдать.","error")
  return redirect(url_for("admin_orders"))
 state=GameState.query.filter_by(user_id=winner.id).first()
 if not state or state.contest_points<=0:
  flash("Нет участника с набранными очками.","error")
  return redirect(url_for("admin_orders"))
 prize=Order(user_id=winner.id,nickname=winner.nickname,email=winner.email,tariff_key="12",amount=0,payment_method="game",status="awaiting_review",requested_days=365)
 db.session.add(prize)
 db.session.flush()
 campaign.winner_user_id=winner.id
 campaign.prize_order_id=prize.id
 db.session.commit()
 notify_admin_telegram(f"🏆 Победитель месячной акции FlankVPN: {winner.nickname}\nОчки: {state.contest_points}\nНаграда: годовая подписка (заказ #{prize.id}). Подтверди заказ в админке, чтобы выдать доступ.")
 flash(f"Победитель: {winner.nickname} ({state.contest_points} очков). Заказ на годовую подписку создан; подтверди его в списке заказов для выдачи доступа.","success")
 return redirect(url_for("admin_orders"))

@app.route("/admin/login",methods=["GET","POST"])
def admin_login():
 if request.method=="POST" and request.form.get("username","").strip().casefold()==os.getenv("ADMIN_USERNAME","admin").strip().casefold() and request.form.get("password","")==os.getenv("ADMIN_PASSWORD","change-this-now"):
  session["flankadmin"]=True; return redirect(url_for("admin_orders"))
 elif request.method=="POST": flash("Неверные данные администратора.","error")
 return render_template("admin_login.html")
@app.route("/admin")
@app.route("/admin/orders")
def admin_orders():
 if not is_admin(): return redirect(url_for("admin_login"))
 orders=Order.query.order_by(Order.created_at.desc()).all(); game_purchases=GamePurchase.query.order_by(GamePurchase.created_at.desc()).all(); support_messages=SupportMessage.query.order_by(SupportMessage.created_at.desc()).all(); campaign=get_game_campaign(); leaderboard=User.query.join(GameState,GameState.user_id==User.id).order_by(GameState.contest_points.desc(),User.nickname.asc()).limit(10).all(); return render_template("admin.html",support_messages=support_messages,game_purchases=game_purchases,orders=orders,campaign=campaign,leaderboard=leaderboard,now=datetime.utcnow(),total_orders=len(orders),total_pending=sum(o.amount for o in orders if o.status=="awaiting_review"),awaiting_payment_count=sum(1 for o in orders if o.status=="awaiting_payment"),awaiting_review_count=sum(1 for o in orders if o.status=="awaiting_review"),active_count=sum(1 for o in orders if o.status=="active"),error_count=sum(1 for o in orders if o.status=="error"))
def provision(o):
 base=os.getenv("H1_API_BASE","").rstrip("/"); token=os.getenv("H1_API_TOKEN","")
 if not base or not token: raise RuntimeError("Настройте H1_API_BASE и H1_API_TOKEN в переменных окружения.")
 name="flank_"+str(o.id)+"_"+''.join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(5)); h={"Authorization":"Bearer "+token,"Content-Type":"application/json"}
 tariff=TARIFFS[o.tariff_key]; days=o.requested_days or tariff.get("days",tariff["months"]*30)
 r=requests.post(base+"/create",headers=h,json={"name":name,"days":days,"traffic_limit_gb":0,"device_limit":0},timeout=25); r.raise_for_status(); d=r.json()
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
 try:
  o.status="processing"; db.session.commit(); provision(o)
  email_sent=send_subscription_email(o)
  notify_admin_telegram(f"✅ Доступ создан для заказа #{o.id}\nНикнейм: {o.nickname}\nEmail: {o.email}\nПисьмо клиенту: {'отправлено' if email_sent else 'НЕ отправлено — проверь Resend и логи Render'}")
  if email_sent:
   flash("Доступ выдан, ссылка подписки отправлена клиенту на email.","success")
  else:
   flash("VPN-доступ создан, но письмо клиенту не отправлено. Проверь настройки RESEND_API_KEY, RESEND_FROM_EMAIL и логи Render.","error")
 except Exception as e:
  db.session.rollback(); o=db.session.get(Order,oid); o.status="error"; o.error_message=str(e)[:400]; db.session.commit(); app.logger.exception("H1 API error")
  notify_admin_telegram(f"❌ Не удалось выдать VPN-доступ для заказа #{o.id}\nНикнейм: {o.nickname}\nПричина: {o.error_message}")
 return redirect(url_for("admin_orders"))
@app.route("/admin/reject/<int:oid>",methods=["POST"])
def reject(oid):
 if not is_admin(): abort(403)
 o=db.session.get(Order,oid)
 if o and o.status=="awaiting_review": o.status="rejected"; db.session.commit()
 return redirect(url_for("admin_orders"))
with app.app_context():
 db.create_all()
 # Add new columns to existing PostgreSQL/SQLite databases without deleting user progress.
 with db.engine.begin() as conn:
  game_columns={column["name"] for column in inspect(conn).get_columns("game_state")}
  if "contest_points" not in game_columns:
   conn.execute(text("ALTER TABLE game_state ADD COLUMN contest_points INTEGER NOT NULL DEFAULT 0"))
  if "redeem_count" not in game_columns:
   conn.execute(text("ALTER TABLE game_state ADD COLUMN redeem_count INTEGER NOT NULL DEFAULT 0"))
  order_columns={column["name"] for column in inspect(conn).get_columns("orders")}
  if "requested_days" not in order_columns:
   conn.execute(text("ALTER TABLE orders ADD COLUMN requested_days INTEGER"))
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.getenv("PORT","5001")),debug=False)
