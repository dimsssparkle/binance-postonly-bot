# Binance Post-Only Bot

FastAPI сервис, принимающий сигналы (TradingView/ручные) и ставящий лимитные **post-only** ордера с быстрым репрайсом (каждые ~200мс), закрывает встречную позицию reduceOnly post-only, затем открывает новую.

## Локальный запуск
1. `python3 -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. Создай `.env` по `.env.example` и заполни ключи.
4. `python main.py` (или `uvicorn main:app --reload`)
5. Health-check: `curl http://127.0.0.1:8000/healthz`

### Тест ручного вызова
curl -X POST http://127.0.0.1:8000/trade/manual
-H "Content-Type: application/json"
-d '{"symbol":"ETHUSDT","side":"long","qty":0.02}'

bash
Copy
Edit

### TradingView
В Alert: Webhook URL = `https://<render-app-url>/tv/webhook`  
Payload (JSON):
{"symbol":"ETHUSDT","side":"short","secret":"<TV_WEBHOOK_SECRET>"}

markdown
Copy
Edit

## Деплой на Render
1. Залей репозиторий на GitHub.
2. На Render -> New -> Web Service -> выбери репо.
3. Укажи `render.yaml` или вручную:
   - Build Command: `pip install -r requirements.txt`
   - Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Добавь env vars (API ключи и SECRET).
5. Дождись зелёного статуса. Проверь `/healthz`.