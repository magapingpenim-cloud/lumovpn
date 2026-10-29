# FlankVPN — сайт-прототип

Flask-проект с подтверждением email через Resend, регистрацией и входом, автоматической 10-дневной бесплатной пробной подпиской после подтверждения email, тарифами 50 ₽/1 месяц, 150 ₽/3 месяца и 600 ₽/12 месяцев, историей заказов, ручной заявкой на оплату СБП/картой, админ-проверкой заказа, интеграцией с H1 API и ссылкой подписки/QR.

## Важно
- Оплата сейчас ручная: пользователь переводит деньги и отправляет заявку, администратор проверяет поступление денег самостоятельно. Автоматической проверки платежа нет.
- Не добавляй в репозиторий `.env`, API-ключи, токены и базу данных. Секреты вводятся в переменные окружения Render.
- `H1_API_BASE` в примере использует HTTP, которое не шифрует bearer-токен. Не используй с реальными клиентами, пока API-подключение не защищено HTTPS.
- Перед реальным публичным запуском добавь CSRF-защиту, ограничения входа, надёжную проверку прав администратора, резервное копирование и постоянную PostgreSQL-базу. Это стартовый прототип, не готовый к коммерческой эксплуатации без дополнительной проверки.

## Локальный запуск (Windows CMD)
1. `py -m venv .venv`
2. `.venv\\Scripts\\activate`
3. `pip install -r requirements.txt`
4. Задай переменные окружения из `.env.example` через `set NAME=value` в CMD и выполни `py app.py`.
5. Сайт: `http://127.0.0.1:5001`; вход администратора: `/admin/login`.

## Render
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- В **Environment** добавь реальные значения `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `H1_API_BASE`, `H1_API_TOKEN`, `H1_SUBSCRIPTION_BASE`, `RESEND_API_KEY`, `RESEND_FROM_EMAIL`, данные оплаты и `DATABASE_URL`.
- Отправитель в Resend: `FlankVPN <no-reply@flankvpn.ru>` — используй его только если домен по-прежнему подтверждён и принадлежит тебе.
- Используй постоянную PostgreSQL-базу; SQLite на файловой системе Render может потеряться при redeploy.

## Напоминания об окончании подписки

- Каждый день GitHub Actions проверяет срок VPN-подписок и отправляет напоминание примерно за 2 дня до окончания.
- Напоминание отправляется на email через Resend и/или в Telegram, если Telegram привязан к аккаунту либо заказ оформлялся через Telegram.
- Для защиты служебного endpoint задай длинное случайное значение `SUBSCRIPTION_REMINDER_SECRET` в **Render → Environment** и такое же значение в **GitHub → Settings → Secrets and variables → Actions → New repository secret** с именем `SUBSCRIPTION_REMINDER_SECRET`.
- После добавления секрета дождись deploy в Render. Проверить запуск можно в **GitHub → Actions → Subscription expiry reminders → Run workflow**.
- Для email нужны рабочие `RESEND_API_KEY` и `RESEND_FROM_EMAIL`; для Telegram — `TELEGRAM_BOT_TOKEN`. Пользователю нужно хотя бы один раз открыть бота и/или привязать Telegram к аккаунту, иначе бот не сможет написать ему первым.

## Telegram-бот FlankVPN

Вебхук бота обслуживается тем же Flask-приложением на Render. В Render → Environment добавь:
- `TELEGRAM_BOT_TOKEN` — токен, который выдал `@BotFather` (он также используется для уведомлений администратора).
- `TELEGRAM_CHAT_ID` — chat ID администратора для уведомлений.
- `TELEGRAM_WEBHOOK_SECRET` — длинная случайная строка для защиты вебхука.
- `TELEGRAM_BOT_USERNAME` — username бота без символа `@`.

После deploy открой `https://flankvpn.ru/telegram/link-code` только после входа в аккаунт — страница создаёт одноразовый код привязки Telegram. В боте отправь `/link КОД`. Код действует 10 минут.

Установи webhook после deploy, подставив свой токен, секрет и фактический публичный адрес сайта:
```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://flankvpn.ru/telegram/webhook/<TELEGRAM_WEBHOOK_SECRET>"}'
```
Не публикуй URL вебхука с секретом и токен бота. Бот поддерживает тарифы, просмотр последних заказов после привязки аккаунта и обращения в поддержку через команду `/support текст`. Уведомления администратора отправляются через существующие `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`.

## Email-подтверждение
Код из 6 цифр живёт 10 минут и ограничен пятью попытками. API-ключ Resend намеренно не сохранён в этом репозитории.

## Уведомления
- Для уведомлений администратору в Telegram добавь в Render переменные `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`. Создай бота через официальный аккаунт `@BotFather`, открой его и нажми Start; токен храни только в Render Environment.
- Бот отправляет сообщения о создании заказа, о том, что клиент заявил об оплате, об успешной выдаче доступа и об ошибке выдачи. Заявка клиента сама по себе не подтверждает поступление денег — перевод нужно проверить в банке вручную.
- После успешного создания VPN-подписки сайт отправляет клиенту email со ссылкой через Resend. Требуются действительные `RESEND_API_KEY` и `RESEND_FROM_EMAIL`.
