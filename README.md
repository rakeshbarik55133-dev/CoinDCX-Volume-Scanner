# CoinDCX Crypto Flag Pattern Screener

यह Python screener CoinDCX के active **USDT spot pairs** में 15-minute flag / sideways-base breakout खोजता है और योग्य signal मिलने पर Telegram alert भेजता है। यह केवल market-data scanner है; यह कोई order place नहीं करता।

## Pattern rules

हर pair के लिए screener:

1. लगातार पिछली **50 closed 15m candles** को sideways base के रूप में जाँचता है।
2. Base की total range midpoint की तुलना में अधिकतम **1.8%** और opening-to-ending drift अधिकतम **0.8%** होना चाहिए।
3. Base में किसी candle का volume base average के **1.6x** से ज्यादा नहीं होना चाहिए।
4. Base बन जाने के बाद अगली/latest 15m candle का high base high के ऊपर (BUY) या low base low के नीचे (SELL) जाना चाहिए।
5. Trigger candle का volume saved base-average volume का कम-से-कम **3x** होना चाहिए।

Alert live/latest trigger candle पर भेजा जाता है—15m candle close का इंतज़ार नहीं किया जाता। Scanner duplicate alerts और saved setups को `.alert_state.json` में रखता है।

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export BOT_TOKEN="your-telegram-bot-token"
export CHAT_ID="your-telegram-chat-id"
python main.py
```

`BOT_TOKEN` और `CHAT_ID` न देने पर scanner dry scan करेगा और alert नहीं भेजेगा।

## Run continuously

By default, scanner एक scan करके exit होता है। प्रत्येक completed scan के एक घंटे बाद फिर से scan करने के लिए:

```bash
RUN_FOREVER=1 python main.py
```

## Optional environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `RUN_FOREVER` | `0` | `1` करने पर हर घंटे scan दोहराता है। |
| `SCAN_SLEEP_SECONDS` | `0.15` | प्रत्येक pair API request के बाद delay। |
| `STATE_FILE` | `.alert_state.json` | Alerts और active setups की state file। |
| `LOG_LEVEL` | `INFO` | Python log level। |

## Tests

```bash
python -m unittest -v
```
