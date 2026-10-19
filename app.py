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
TARIFFS={"trial":{"months":0,"days":10,"price":0,"title":"Бесплатный пробный период — 10 дней"},"1":{"months":1,"price":129,"title":"1 месяц","discount":0,"save":0},"3":{"months":3,"price":348,"title":"3 месяца","discount":10,"save":39},"12":{"months":12,"price":1238,"title":"12 месяцев","discount":20,"save":310}}
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
 id=db.Column(db.Integer,primary_key=True); user_id=db.Column(db.Integer,db.ForeignKey("user.id"),nullable=False); nickname=db.Column(db.String(40),nullable=False); email=db.Column(db.String(160),nullable=False); tariff_key=db.Column(db.String(8),nullable=False); amount=db.Column(db.Integer,nullable=False); payment_method=db.Column(db.String(12),nullable=False); status=db.Column(db.String(24),default="awaiting_payment"); created_at=db.Column(db.DateTime,default=datetime.utcnow); subscription_url=db.Column(db.Text); error_message=db.Column(db.Text); requested_days=db.Column(db.Integer); telegram_id=db.Column(db.String(40),nullable=True,index=True)
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
 reply=db.Column(db.Text,nullable=True)
 replied_at=db.Column(db.DateTime,nullable=True)
 status=db.Column(db.String(20),nullable=False,default="new")
 created_at=db.Column(db.DateTime,default=datetime.utcnow,nullable=False)
 user=db.relationship("User",backref=db.backref("support_messages",lazy=True))


class TelegramAccount(db.Model):
 id=db.Column(db.Integer,primary_key=True)
 telegram_id=db.Column(db.String(40),unique=True,nullable=False,index=True)
 user_id=db.Column(db.Integer,db.ForeignKey("user.id"),unique=True,nullable=False)
 linked_at=db.Column(db.DateTime,default=datetime.utcnow,nullable=False)
 user=db.relationship("User",backref=db.backref("telegram_account",uselist=False))

class TelegramCheckoutDraft(db.Model):
 id=db.Column(db.Integer,primary_key=True)
 telegram_id=db.Column(db.String(40),unique=True,nullable=False,index=True)
 nickname=db.Column(db.String(40),nullable=True)
 pending_tariff=db.Column(db.String(8),nullable=True)
 awaiting_nickname=db.Column(db.Boolean,default=False,nullable=False)
 updated_at=db.Column(db.DateTime,default=datetime.utcnow,nullable=False)

class TelegramLinkCode(db.Model):
 id=db.Column(db.Integer,primary_key=True)
 user_id=db.Column(db.Integer,db.ForeignKey("user.id"),nullable=False,index=True)
 code=db.Column(db.String(12),unique=True,nullable=False,index=True)
 expires_at=db.Column(db.DateTime,nullable=False)
 used=db.Column(db.Boolean,default=False,nullable=False)

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

@app.route("/telegram/link-code")
@login_required
def telegram_link_code():
 code="".join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(8))
 TelegramLinkCode.query.filter_by(user_id=current_user.id,used=False).delete()
 db.session.add(TelegramLinkCode(user_id=current_user.id,code=code,expires_at=datetime.utcnow()+timedelta(minutes=10)))
 db.session.commit()
 bot_name=os.getenv("TELEGRAM_BOT_USERNAME","").strip().lstrip("@")
 bot_link=("https://t.me/"+bot_name) if bot_name else "Открой своего Telegram-бота FlankVPN"
 return (f"<h1>Привязка Telegram — FlankVPN</h1><p>Твой одноразовый код: <strong style='font-size:28px'>{code}</strong></p><p>Код действует 10 минут. В Telegram отправь боту команду <code>/link {code}</code>.</p><p>Не пересылай код другим людям.</p><p><a href='{bot_link}'>Открыть Telegram-бота</a></p><p><a href='/dashboard'>Вернуться в кабинет</a></p>",200,{"Content-Type":"text/html; charset=utf-8"})

@app.route("/telegram/webhook/<secret>",methods=["POST"])
def telegram_webhook(secret):
 expected=os.getenv("TELEGRAM_WEBHOOK_SECRET","").strip()
 if not expected or not secrets.compare_digest(secret,expected):
  abort(404)
 token=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
 if not token:
  app.logger.error("Telegram webhook called but TELEGRAM_BOT_TOKEN is missing")
  return {"ok":False},503
 update=request.get_json(silent=True) or {}
 callback=update.get("callback_query") or {}
 message=update.get("message") or update.get("edited_message") or callback.get("message") or {}
 chat=message.get("chat") or {}
 chat_id=chat.get("id")
 from_user=callback.get("from") or message.get("from") or {}
 telegram_id=str(from_user.get("id",""))
 text_in=(message.get("text") or "").strip()
 callback_data=(callback.get("data") or "").strip()
 if not chat_id or not telegram_id or (not text_in and not callback_data):
  return {"ok":True}
 def answer_callback(text_out=""):
  callback_id=callback.get("id")
  if not callback_id: return
  try:
   requests.post("https://api.telegram.org/bot"+token+"/answerCallbackQuery",
    json={"callback_query_id":callback_id,"text":text_out[:180]},timeout=8).raise_for_status()
  except Exception:
   app.logger.exception("Telegram callback acknowledgement failed")
 def send(text_out,keyboard=True,inline=None):
  payload={"chat_id":chat_id,"text":text_out,"disable_web_page_preview":True}
  if inline is not None:
   payload["reply_markup"]={"inline_keyboard":inline}
  elif keyboard:
   payload["reply_markup"]={"keyboard":[[{"text":"🌐 Тарифы"},{"text":"📦 Мои заказы"}],[{"text":"💬 Поддержка"},{"text":"🔗 Привязать аккаунт"}]],"resize_keyboard":True}
  try:
   resp=requests.post("https://api.telegram.org/bot"+token+"/sendMessage",json=payload,timeout=8)
   resp.raise_for_status()
   if not resp.json().get("ok"): raise RuntimeError("Telegram sendMessage returned ok=false")
  except Exception:
   app.logger.exception("Telegram bot could not reply to chat %s",chat_id)
 def show_tariffs():
  rows=[]
  for key in ("1","3","12"):
   tariff=TARIFFS[key]
   rows.append([{"text":f"{tariff['title']} — {tariff['price']} ₽","callback_data":"tariff:"+key}])
  send("Выбери тариф FlankVPN:",keyboard=False,inline=rows)
 def show_payment_methods(key):
  tariff=TARIFFS.get(key)
  if not tariff or key not in ("1","3","12"):
   send("Неизвестный тариф. Открой раздел «Тарифы» ещё раз.")
   return
  send(f"Тариф: {tariff['title']}\nСтоимость: {tariff['price']} ₽\n\nВыбери способ оплаты:",keyboard=False,
   inline=[[{"text":"Перевод по номеру (СБП)","callback_data":f"buy:{key}:sbp"}],
           [{"text":"Банковская карта","callback_data":f"buy:{key}:card"}],
           [{"text":"⬅️ К тарифам","callback_data":"show_tariffs"}]])
 cmd=text_in.split(maxsplit=1)[0].lower() if text_in else ""
 arg=text_in.split(maxsplit=1)[1].strip() if len(text_in.split(maxsplit=1))>1 else ""
 if callback_data:
  answer_callback()
  if callback_data=="show_tariffs":
   show_tariffs()
  elif callback_data.startswith("tariff:"):
   key=callback_data.split(":",1)[1]
   if key not in ("1","3","12"):
    send("Неизвестный тариф. Открой раздел «Тарифы» ещё раз.")
   else:
    draft=TelegramCheckoutDraft.query.filter_by(telegram_id=telegram_id).first()
    if not draft:
     draft=TelegramCheckoutDraft(telegram_id=telegram_id)
     db.session.add(draft)
    draft.pending_tariff=key; draft.nickname=None; draft.awaiting_nickname=True; draft.updated_at=datetime.utcnow()
    db.session.commit()
    tariff_title=TARIFFS[key]["title"]
    send(f"Ты выбрал тариф «{tariff_title}».\n\nВведите свой ник, который вы будете использовать при оплате. Важно: при переводе укажи именно этот ник, без изменений.")

  elif callback_data.startswith("buy:"):
   parts=callback_data.split(":")
   if len(parts)!=3 or parts[1] not in ("1","3","12") or parts[2] not in PAYMENTS:
    send("Не удалось определить тариф или способ оплаты. Попробуй ещё раз.")
   else:
    key=parts[1]; method=parts[2]; tariff=TARIFFS[key]
    draft=TelegramCheckoutDraft.query.filter_by(telegram_id=telegram_id).first()
    if not draft or not draft.nickname or draft.pending_tariff!=key or draft.awaiting_nickname:
     send("Сначала выбери тариф и введи ник, который укажешь при оплате.")
    else:
     nickname=draft.nickname
     shadow_email="telegram_"+telegram_id+"@telegram.flankvpn.local"
     shadow=User.query.filter_by(email=shadow_email).first()
     if not shadow:
      internal_nick=("tg_"+telegram_id[-30:])[:40]
      if User.query.filter_by(nickname=internal_nick).first(): internal_nick=("tg_"+telegram_id[-20:]+"_"+secrets.token_hex(3))[:40]
      shadow=User(nickname=internal_nick,email=shadow_email,password_hash=generate_password_hash(secrets.token_urlsafe(32)))
      db.session.add(shadow); db.session.flush()
     order=Order(user_id=shadow.id,nickname=nickname,email="",telegram_id=telegram_id,tariff_key=key,amount=tariff["price"],payment_method=method,status="awaiting_payment")
     db.session.add(order)
     try:
      db.session.commit()
      notify_admin_telegram(f"🆕 Новый заказ из Telegram FlankVPN #{order.id}\nНикнейм: {order.nickname}\nEmail: {order.email}\nTelegram ID: {telegram_id}\nТариф: {tariff['title']}\nСумма: {order.amount} ₽\nСпособ: {PAYMENTS[method]['title']}\nСтатус: ожидает оплаты")
      details=PAYMENTS[method]["detail"]
      recipient=os.getenv("PAYMENT_RECIPIENT","").strip() or "Получатель не настроен — свяжись с поддержкой"
      months=tariff.get("months",0)
      payment_text=(
       f"🧾 ЗАКАЗ FLANKVPN #{order.id}\n\n"
       f"Тариф: {tariff['title']}\n"
       f"К оплате: {order.amount} ₽\n"
       f"Способ оплаты: {PAYMENTS[method]['title']}\n\n"
       f"💳 РЕКВИЗИТЫ ДЛЯ ПЕРЕВОДА\n{details}\n"
       f"Получатель: {recipient}\n\n"
       f"⚠️ ОБЯЗАТЕЛЬНО УКAЖИ НИК В СООБЩЕНИИ К ПЕРЕВОДУ\n"
       f"Твой ник: {order.nickname}\n"
       f"Укажи именно «{order.nickname}», без изменений. Не указывай вместо него имя или email. Если поле сообщения к переводу доступно, впиши ник туда.\n\n"
       f"📋 ЧТО ДЕЛАТЬ\n"
       f"1. Переведи ровно {order.amount} ₽ по указанным реквизитам.\n"
       f"2. В сообщении к переводу укажи ник «{order.nickname}».\n"
       f"3. Нажми «Я оплатил» только после фактической оплаты.\n\n"
       f"После нажатия администратор вручную проверит поступление денег. Само нажатие не подтверждает оплату; доступ выдаётся после проверки."
      )
      send(payment_text,keyboard=False,
       inline=[[{"text":"💳 Я оплатил","callback_data":f"paid:{order.id}"}],
               [{"text":"🌐 Открыть сайт","url":"https://flankvpn.ru"}]])
     except Exception:
      db.session.rollback()
      app.logger.exception("Could not create Telegram order")
      send("Не получилось создать заказ. Попробуй ещё раз или напиши в поддержку.")
  elif callback_data.startswith("paid:"):
   try: order_id=int(callback_data.split(":",1)[1])
   except (ValueError,IndexError): order_id=0
   order=db.session.get(Order,order_id) if order_id else None
   if not order or order.telegram_id!=telegram_id:
    send("Не удалось найти твой заказ. Открой «Мои заказы» или напиши в поддержку.")
   elif order.status=="awaiting_payment":
    order.status="awaiting_review"
    try:
     db.session.commit()
     tariff_title=TARIFFS.get(order.tariff_key,{}).get("title",order.tariff_key)
     method_title=PAYMENTS.get(order.payment_method,{}).get("title",order.payment_method)
     notify_admin_telegram(f"💳 Клиент нажал «Я оплатил» — заказ #{order.id}\nНик для сверки перевода: {order.nickname}\nТариф: {tariff_title}\nСумма: {order.amount} ₽\nСпособ: {method_title}\nTelegram ID: {telegram_id}\nСтатус: проверить поступление денег вручную.")
     send(f"Заявка на проверку оплаты заказа #{order.id} отправлена. Доступ будет подготовлен после подтверждения администратором.")
    except Exception:
     db.session.rollback()
     app.logger.exception("Could not mark Telegram order as awaiting review")
     send("Не получилось отправить заявку. Попробуй ещё раз.")
   elif order.status=="awaiting_review":
    send(f"Заказ #{order.id} уже ожидает проверки оплаты.")
   else:
    send(f"Текущий статус заказа #{order.id}: {order.status}. Если нужна помощь, напиши в поддержку.")
  return {"ok":True}
 draft=TelegramCheckoutDraft.query.filter_by(telegram_id=telegram_id).first()
 if draft and draft.awaiting_nickname and text_in and not text_in.startswith("/") and text_in not in ("🌐 Тарифы","📦 Мои заказы","💬 Поддержка","🔗 Привязать аккаунт","🏠 Главное меню"):
  nickname=text_in.strip()
  if len(nickname)<2 or len(nickname)>40 or "\n" in nickname:
   send("Ник должен быть длиной от 2 до 40 символов. Введи ник ещё раз.")
  else:
   draft.nickname=nickname; draft.awaiting_nickname=False; draft.updated_at=datetime.utcnow(); db.session.commit()
   send(f"Ник для оплаты сохранён: {nickname}. Используй именно его в комментарии к переводу. Теперь выбери способ оплаты.")
   show_payment_methods(draft.pending_tariff)
  return {"ok":True}
 if cmd=="/start" or text_in=="🏠 Главное меню":
  send("Привет! Это FlankVPN. Здесь можно выбрать тариф, оформить заказ, проверить его статус и написать в поддержку.")
 elif text_in in ("🌐 Тарифы","/tariffs"):
  show_tariffs()
 elif text_in in ("📦 Мои заказы","/orders"):
  orders=Order.query.filter_by(telegram_id=telegram_id).order_by(Order.created_at.desc()).limit(10).all()
  if not orders: send("Заказов пока нет. Выбери «🌐 Тарифы», чтобы оформить подписку.")
  else:
   labels={"awaiting_payment":"ожидает оплаты","awaiting_review":"проверяется оплата","processing":"создаётся доступ","active":"активна","rejected":"отклонён","error":"ошибка выдачи"}
   lines=["Твои последние заказы:"]
   for order in orders:
    title=TARIFFS.get(order.tariff_key,{}).get("title","Заказ")
    lines.append(f"#{order.id} · {title} · {order.amount} ₽ · {labels.get(order.status,order.status)}")
   send("\n".join(lines))
 elif cmd=="/link" and arg:
  link=TelegramLinkCode.query.filter_by(code=arg.upper(),used=False).first()
  if not link or link.expires_at<datetime.utcnow():
   send("Код не найден или истёк. Войди на flankvpn.ru и создай новый код привязки.")
  else:
   existing=TelegramAccount.query.filter_by(telegram_id=telegram_id).first()
   account_for_user=TelegramAccount.query.filter_by(user_id=link.user_id).first()
   if account_for_user and account_for_user.telegram_id!=telegram_id:
    send("К этому аккаунту FlankVPN уже привязан другой Telegram. Обратись в поддержку для смены привязки.")
   elif existing and existing.user_id!=link.user_id:
    send("Этот Telegram уже привязан к другому аккаунту FlankVPN. Сначала отвяжи его через поддержку.")
   else:
    if not existing: db.session.add(TelegramAccount(telegram_id=telegram_id,user_id=link.user_id))
    link.used=True
    try:
     db.session.commit()
     send("Готово! Telegram привязан к аккаунту FlankVPN. Теперь можешь выбрать тариф и оформить заказ.")
    except Exception:
     db.session.rollback()
     app.logger.exception("Could not link Telegram account")
     send("Не удалось привязать аккаунт. Попробуй ещё раз.")
 elif text_in in ("🔗 Привязать аккаунт","/link"):
  send("Войди на https://flankvpn.ru, затем открой https://flankvpn.ru/telegram/link-code и отправь боту команду /link КОД.")
 elif text_in in ("💬 Поддержка","/support"):
  send("Чтобы написать в поддержку, отправь сообщение командой:\n/support Твой вопрос")
 elif cmd=="/support" and len(arg)>=3:
  account=TelegramAccount.query.filter_by(telegram_id=telegram_id).first()
  user=db.session.get(User,account.user_id) if account else None
  username=(from_user.get("username") or "").strip()
  nickname=user.nickname if user else ("@"+username if username else str(from_user.get("first_name") or "Telegram-клиент"))
  email=user.email if user else ""
  item=SupportMessage(user_id=user.id if user else None,nickname=nickname,email=email,message="[Telegram] "+arg[:1900])
  db.session.add(item)
  try:
   db.session.commit()
   notify_admin_telegram(f"✉️ Обращение из Telegram #{item.id}\nОт: {nickname}\nEmail: {email or 'не привязан'}\nСообщение: {arg[:1000]}")
   send("Сообщение отправлено в поддержку FlankVPN. Мы получили обращение.")
  except Exception:
   db.session.rollback()
   app.logger.exception("Could not save Telegram support message")
   send("Не получилось сохранить обращение. Попробуй ещё раз позже.")
 else:
  send("Выбери пункт меню или используй /start.")
 return {"ok":True}

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
@app.route("/payment-sbp-qr.png")
def payment_sbp_qr():
 payment_url="https://www.tbank-online.com/rm/r_vAnkhgISyw.ZkhuMWpXtK/UbHFv58761"
 image=qrcode.make(payment_url)
 output=io.BytesIO()
 image.save(output,format="PNG")
 output.seek(0)
 return send_file(output,mimetype="image/png",max_age=3600)

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
 leaderboard=User.query.join(GameState,GameState.user_id==User.id).order_by(GameState.contest_points.desc(),User.nickname.asc()).limit(100).all()
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
  try:
   purchase.status="awaiting_review"
   db.session.commit()
  except Exception:
   db.session.rollback()
   app.logger.exception("Could not mark autoclicker purchase %s as awaiting review",pid)
   flash("Не удалось отправить заявку. Попробуй ещё раз; если ошибка повторится, сообщи администратору.", "error")
   return redirect(url_for("game_payment",pid=pid))
  message=(f"💳 Клиент сообщил об оплате автокликера\nЗаявка #{purchase.id}\n"
           f"Никнейм: {purchase.nickname}\nEmail: {purchase.email}\n"
           f"Сумма: {purchase.amount} ₽\nСпособ: {PAYMENTS[purchase.payment_method]['title']}\n"
           "Проверь поступление денег в панели FlankVPN → Заказы и оплаты → Заявки на автокликер.")
  notify_admin_telegram(message)
  app.logger.info("Autoclicker purchase %s submitted for review by user %s",purchase.id,current_user.id)
  flash("Заявка отправлена на проверку. Она должна появиться в админке в разделе «Заявки на автокликер».", "success")
 elif purchase.status=="awaiting_review":
  flash("Эта заявка уже отправлена и ожидает проверки.", "success")
 elif purchase.status=="active":
  flash("Автокликер уже активирован.", "success")
 else:
  flash("Эта заявка больше не ожидает оплаты. Если это ошибка, обратись к администратору.", "error")
 return redirect(url_for("game_payment",pid=pid))

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

@app.route("/admin/support/<int:mid>/reply",methods=["POST"])
def admin_reply_support(mid):
 if not is_admin(): abort(403)
 item=db.session.get(SupportMessage,mid)
 if not item: abort(404)
 reply=request.form.get("reply","").strip()
 if len(reply)<1 or len(reply)>4000:
  flash("Ответ должен содержать от 1 до 4000 символов.","error")
  return redirect(url_for("admin_support"))
 item.reply=reply
 item.replied_at=datetime.utcnow()
 item.status="closed"
 try:
  db.session.commit()
 except Exception:
  db.session.rollback()
  app.logger.exception("Could not save support reply for message %s",mid)
  flash("Не удалось сохранить ответ. Попробуй ещё раз.","error")
  return redirect(url_for("admin_support"))
 flash("Ответ сохранён в разделе клиента «Мои вопросы».","success")
 return redirect(url_for("admin_support"))

@app.route("/support/my")
@login_required
def my_support():
 messages=SupportMessage.query.filter_by(user_id=current_user.id).order_by(SupportMessage.created_at.desc()).all()
 return render_template("my_support.html",support_messages=messages)

@app.route("/admin/support/<int:mid>/close",methods=["POST"])
def admin_close_support(mid):
 if not is_admin(): abort(403)
 item=db.session.get(SupportMessage,mid)
 if not item: abort(404)
 item.status="closed"
 db.session.commit()
 flash("Обращение отмечено как обработанное.","success")
 return redirect(url_for("admin_support"))

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
 orders=Order.query.order_by(Order.created_at.desc()).all(); game_purchases=GamePurchase.query.order_by(GamePurchase.created_at.desc()).all(); campaign=get_game_campaign(); leaderboard=User.query.join(GameState,GameState.user_id==User.id).order_by(GameState.contest_points.desc(),User.nickname.asc()).limit(10).all(); support_new_count=SupportMessage.query.filter_by(status="new").count(); return render_template("admin.html",support_new_count=support_new_count,game_purchases=game_purchases,orders=orders,campaign=campaign,leaderboard=leaderboard,now=datetime.utcnow(),total_orders=len(orders),total_pending=sum(o.amount for o in orders if o.status=="awaiting_review"),awaiting_payment_count=sum(1 for o in orders if o.status=="awaiting_payment"),awaiting_review_count=sum(1 for o in orders if o.status=="awaiting_review"),active_count=sum(1 for o in orders if o.status=="active"),error_count=sum(1 for o in orders if o.status=="error"))
@app.route("/admin/support")
def admin_support():
 if not is_admin(): return redirect(url_for("admin_login"))
 support_messages=SupportMessage.query.order_by(SupportMessage.created_at.desc()).all()
 return render_template("admin_support.html",support_messages=support_messages)
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
  email_sent=send_subscription_email(o) if o.email else False
  telegram_sent=False
  if o.telegram_id and o.subscription_url:
   bot_token=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
   if bot_token:
    try:
     tg_title=TARIFFS.get(o.tariff_key,{}).get("title",o.tariff_key)
     tg_resp=requests.post("https://api.telegram.org/bot"+bot_token+"/sendMessage",json={"chat_id":o.telegram_id,"text":f"✅ Оплата заказа #{o.id} подтверждена! Твоя подписка FlankVPN готова.\n\nТариф: {tg_title}\nНик: {o.nickname}\nСсылка для подключения:\n{o.subscription_url}\n\nДобавь ссылку в совместимое VPN-приложение.","disable_web_page_preview":True},timeout=10)
     tg_resp.raise_for_status(); telegram_sent=bool(tg_resp.json().get("ok"))
    except Exception:
     app.logger.exception("Could not send subscription to Telegram for order %s",o.id)
  notify_admin_telegram(f"✅ Доступ создан для заказа #{o.id}\nНикнейм: {o.nickname}\nEmail: {o.email or 'не указан (Telegram)'}\nДоставка: {'Telegram отправлено' if telegram_sent else ('email отправлено' if email_sent else 'не удалось отправить клиенту — проверь логи Render')}")
  if telegram_sent or email_sent:
   flash("Доступ выдан, ссылка подписки отправлена клиенту.","success")
  else:
   flash("VPN-доступ создан, но сообщение со ссылкой клиенту не отправлено. Проверь токен Telegram и логи Render.","error")
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
 # Add new columns only when the target tables exist; never fail startup on schema inspection.
 with db.engine.begin() as conn:
  inspector=inspect(conn)
  if inspector.has_table(GameState.__tablename__):
   game_columns={column["name"] for column in inspector.get_columns(GameState.__tablename__)}
   if "contest_points" not in game_columns:
    conn.execute(text(f'ALTER TABLE "{GameState.__tablename__}" ADD COLUMN contest_points INTEGER NOT NULL DEFAULT 0'))
   if "redeem_count" not in game_columns:
    conn.execute(text(f'ALTER TABLE "{GameState.__tablename__}" ADD COLUMN redeem_count INTEGER NOT NULL DEFAULT 0'))
  support_table=SupportMessage.__tablename__
  if inspector.has_table(support_table):
   support_columns={column["name"] for column in inspector.get_columns(support_table)}
   if "reply" not in support_columns:
    conn.execute(text('ALTER TABLE "'+support_table+'" ADD COLUMN reply TEXT'))
   if "replied_at" not in support_columns:
    conn.execute(text('ALTER TABLE "'+support_table+'" ADD COLUMN replied_at TIMESTAMP'))
  order_table=Order.__tablename__
  if inspector.has_table(order_table):
   order_columns={column["name"] for column in inspector.get_columns(order_table)}
   if "requested_days" not in order_columns:
    conn.execute(text(f'ALTER TABLE "{order_table}" ADD COLUMN requested_days INTEGER'))
   if "telegram_id" not in order_columns:
    conn.execute(text(f'ALTER TABLE "{order_table}" ADD COLUMN telegram_id VARCHAR(40)'))
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.getenv("PORT","5001")),debug=False)
