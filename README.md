# COPETRANOVAX // QUANTUM SIGNAL BOT & HUD
**High-Accuracy M15 Gold (XAUUSD) Signal Provider // Scalping & Intra-Swing**

Mfumo wa kisasa wa kutoa signals za Dhahabu (`XAUUSD`) kila baada ya dakika 15 zenye kiwango cha usahihi (**Accuracy %**), maelekezo rahisi ya biashara, na **Robotic Cyberpunk HUD Dashboard**.

---

## 🚀 Sifa Kuu (Key Features)

- ⚡ **Dual Trading Modes**:
  - **SCALPING**: Fursa za haraka (Pips 15–30, SL fupi chini ya mshumaa).
  - **INTRA-SWING**: Fursa za masaa mengi (Pips 45–100, RR 1:2.0 hadi 1:3.0).
- 🎯 **Confirmed Accuracy %**: Kila signal inakuja na asilimia ya uhakika (mfano: `89.5%`, `94.5%`) na Grade (`A+ Sniper`, `A High Probability`).
- 🤖 **Robotic Cyberpunk HUD Display**: Dashboard ya kisasa yenye font za sci-fi (`Orbitron`), saa inayohesabu sekunde za mshumaa wa M15 (Countdown Timer), na Live Price ya Gold.
- 📱 **Telegram Alerts Integration**: Uwezo wa kutuma signals moja kwa moja kwenye Telegram Channel au Group lako ili watu wanakili (Copy & Paste).
- 🧠 **Human-Like Reasoning**: Hutoa maelezo ya wazi ya mwanadamu akieleza kwanini trade inafunguliwa (Price action, session, volume, n.k.).

---

## 🛠️ Jinsi ya Kuanza (Quick Start)

### 1. Mahitaji (Prerequisites)
Hakikisha una Python 3.10+ imewekwa:
```bash
pip install pandas numpy
```
*(Kama una MetaTrader 5, weka pia: `pip install MetaTrader5`, lakini kama huna MT5 mfumo bado unatoa signals!)*

### 2. Kuwasha Bot & Robotic HUD
Endesha amri hii moja tu:
```bash
python live_bot.py
```
Hii itawasha bot ya signals NA itafungua Robotic HUD Dashboard moja kwa moja chinichini!

### 3. Kufungua Dashboard kwenye Kivinjari (Browser)
Fungua browser yako (Chrome, Edge, Firefox au simu) nenda:
👉 **`http://localhost:8080`**

---

## 📲 Kutuma Signals Telegram (Hiari / Optional)
Ikiwa unataka bot itume signals moja kwa moja kwenye Group au Channel ya Telegram ili watu watrade:
Weka Token na Chat ID yako kwenye `live_bot.py` au kama environment variables:
```bash
set TELEGRAM_BOT_TOKEN="yako_hapa"
set TELEGRAM_CHAT_ID="group_au_channel_id_hapa"
```
Kila mshumaa wa M15 unapofungwa, signal itatoka na kujituma Telegram moja kwa moja!

---

## 📁 Faili za Signals
- `data/signals.json` : Inatumiwa na HUD Dashboard kwa ajili ya live updates na history.
- `data/signal.txt` : Inatumiwa na MetaTrader EA kwa ajili ya auto-execution.
- `dashboard.html` : UI ya kielektroniki yenye muundo wa robotic cyberpunk.

---

**Developed for Traders // Plug & Play High Performance Engine**
