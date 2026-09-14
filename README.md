# COPETRANOVAX // MULTI-PAIR QUANTUM SIGNAL BOT & ROBOTIC HUD
**High-Accuracy Multi-Pair Engine (XAUUSD, EURUSD, GBPUSD, USDJPY) // Scalping & Intra-Swing**

Mfumo wa kisasa wa kutoa signals za pairs 4 zenye ukwasi mkubwa zaidi duniani (Gold na Major Currencies) kila baada ya dakika 15 zenye kiwango cha usahihi (Accuracy %), shabaha mbili za faida (Dual TP1 & TP2), Fast Breakeven Defense, na Robotic Cyberpunk HUD Dashboard.

---

## Sifa Kuu (Key Features)

- **Multi-Pair Engine (Strict State Isolation)**:
  - Inasoma na kuchambua pairs 4: **XAUUSD (Gold)**, **EURUSD**, **GBPUSD**, na **USDJPY**.
  - Kila pair ina chumba chake huru cha StructureState, pip size, na spread shield ili bot isichanganyikiwe kamwe.
  - Vigezo vya ATR vimebadilishwa kupima pips (atr / pip_sz) ili pairs zote zitoe matokeo safi.

- **Dual Take Profit (TP1 & TP2)**:
  - **TP1 (Quick Bank - 50%)**: Funga nusu ya faida mapema soko linaposogea upande wako.
  - **TP2 (Runner Target)**: Acha lot iliyobaki ifuate trend ndefu kwa Risk-Reward kubwa (1:2.5 hadi 1:3.0+).

- **Fast Breakeven Defense**:
  - Faida ikifika pips +10 hadi +12 (au 0.8R), Stop Loss inasogezwa mara moja kwenye Entry (Risk-Free Trade) ili kulinda mtaji 100%.

- **Confirmed Accuracy % na Daraja**:
  - Kila signal inakuja na asilimia ya uhakika (mfano: 88.5%, 94.5%) na Grade (A+ Sniper, A High Confluence).

- **Robotic Cyberpunk HUD Display**:
  - Dashboard ya kisasa yenye font za sci-fi (Orbitron & Share Tech Mono), Multi-Pair Top Ticker Bar inayoonyesha bei za moja kwa moja na hadhi ya spread, countdown timer ya mshumaa wa M15, na historia ya signals zilizopita.

- **Human-Like Reasoning**:
  - Hutoa maelezo ya wazi ya mwanadamu akieleza kwanini trade inafunguliwa (Price action, session, volume surge, SMC confluence).

- **Telegram Alerts Integration**:
  - Uwezo wa kutuma signals moja kwa moja kwenye Telegram Channel au Group lako zikiwa na muundo safi wa kunakili (Copy & Paste).

---

## Jinsi ya Kuanza (Quick Start)

### 1. Mahitaji (Prerequisites)
Hakikisha una Python 3.10+ imewekwa:
```bash
pip install pandas numpy
```
*(Kama unatumia MetaTrader 5 terminal, unaweza kuweka pia `pip install MetaTrader5`. Kama huna MT5, bot inafanya kazi kwenye Signal-Only Mode kikamilifu!)*

### 2. Kuwasha Bot & Robotic HUD
Endesha amri hii kwenye terminal:
```bash
python live_bot.py
```
Amri hii itaanza kuchambua pairs zote 4 NA itafungua Robotic HUD Dashboard moja kwa moja chinichini kwenye port 8080!

### 3. Kufungua Dashboard kwenye Kivinjari (Browser)
Fungua browser yako (Chrome, Edge, Firefox au simu) nenda:
**http://localhost:8080**

---

## Kutuma Signals Telegram (Hiari / Optional)
Kama unataka bot itume signals moja kwa moja kwenye Group au Channel ya Telegram:
Weka Token na Chat ID yako kwenye `live_bot.py` au kama environment variables:
```bash
set TELEGRAM_BOT_TOKEN="token_yako_hapa"
set TELEGRAM_CHAT_ID="group_au_channel_id_hapa"
```
Kila mshumaa wa M15 unapofungwa, signal itatoka na kujituma Telegram ikiwa na maelekezo kamili ya Entry, Stop Loss, TP1, na TP2!

---

## Muundo wa Faili
- `dashboard.html` : UI ya kielektroniki yenye muundo wa robotic cyberpunk na multi-pair ticker bar.
- `dashboard_server.py` : Server nyepesi inayohudumia Dashboard kwenye `http://localhost:8080`.
- `live_bot.py` : Engine kuu inayochambua pairs zote 4, kutoa signals, na kutuma alerts.
- `risk_engine.py` : Mfumo wa usalama wa mtaji, pip sizes, spread shield, na trade management.
- `structure_engine.py` : SMC Market Structure (BOS, Displacement, Sweeps, Retest).
- `data/signals.json` : Inatumiwa na HUD Dashboard kwa ajili ya live updates na history.
- `data/signal.txt` : Faili la signal ya sasa kwa ajili ya usomaji wa nje.

---

**Developed for Traders // Plug & Play High Performance Multi-Pair Engine**
