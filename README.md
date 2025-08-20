# Binance Post-Only Bot (Render deploy)

## Быстрый старт (Render Blueprints)
1) Подключите репозиторий к Render.
2) Убедитесь, что в корне лежит `render.yaml` (этот файл готов).
3) В Render нажмите **New > Blueprint**, выберите этот репозиторий, подтвердите конфигурацию.
4) В настройках сервиса на вкладке **Environment** задайте значения для:
   - `BINANCE_API_KEY` (secret)
   - `BINANCE_API_SECRET` (secret)
   - `TV_WEBHOOK_SECRET` (secret)
   Остальные переменные уже заданы в `render.yaml` и могут быть изменены при необходимости.
5) Деплой запустится автоматически. Health check: `GET /healthz`.
6) Откройте UI: `https://<your-service>.onrender.com/orders.html?symbol=ETHUSDT`  
   или просто корень `https://<your-service>.onrender.com/` (редирект на дашборд).

## Webhook из TradingView
- URL: `https://<your-service>.onrender.com/webhook`
- Для доп. защиты используйте заголовок `X-Webhook-Secret: <TV_WEBHOOK_SECRET>` или поле `secret` в JSON.
- Пример тела:
```json
{ "symbol": "ETHUSDT", "side": "long", "secret": "<TV_WEBHOOK_SECRET>" }
