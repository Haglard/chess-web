# chess-web

Applicazione web multiplayer per giocare a scacchi nel browser, con motore scacchistico,
supporto ChatGPT e sistema di autenticazione utenti.

Autori: **Sandro Borioni**, **Claude**, **ChatGPT**
Versione: **1.1** -- Licenza: **MIT**

---

## Funzionalita'

### Modalita' di gioco

| Modalita' | Descrizione |
|---|---|
| **UvsU** | Umano vs Umano (stesso browser o dispositivi diversi) |
| **UvsC** | Umano vs Motore scacchistico |
| **UvChatGPT** | Umano vs ChatGPT (gpt-4o-mini) |
| **CvChatGPT** | Motore scacchistico vs ChatGPT |
| **CvC** | Motore scacchistico vs Motore scacchistico |

### Utenti e profili
- Registrazione con email, alias e password
- Login / logout con cookie JWT (30 giorni)
- Reset password via email
- Pagina profilo con statistiche (vittorie/pareggi/sconfitte)
- Storico partite cliccabile

### Motore scacchistico
- Backend: [chess-engine](https://github.com/Haglard/chess-engine) (`search_cli`)
- Profondita' di ricerca configurabile (7-11 ply)
- Valutazione non deterministica (+/-10 cp di rumore) per partite sempre diverse
- Fallback a mossa casuale in caso di timeout

### Interfaccia
- Scacchiera drag-and-drop (chessboard.js + chess.js)
- Aggiornamenti in tempo reale via WebSocket
- Pannello statistiche motore (profondita', score, nps, nodi)
- Pannello ragionamento ChatGPT
- Lobby partite con alias cliccabili
- Tema scuro responsive

---

## Requisiti

- Python 3.11+
- Il binario `../chess-engine/build/search_cli` compilato (vedi [chess-engine](https://github.com/Haglard/chess-engine))
- Chiave API OpenAI (per le modalita' con ChatGPT)

---

## Installazione

```bash
git clone https://github.com/Haglard/chess-web.git
cd chess-web

# Crea e attiva il virtualenv
python3 -m venv venv
source venv/bin/activate

# Installa le dipendenze
pip install -r requirements.txt

# Configura le variabili d'ambiente
cp .env.example .env   # poi edita .env con le tue chiavi
```

### .env

```
OPENAI_API_KEY=sk-...
SECRET_KEY=<stringa-random-hex-32-byte>
```

Genera `SECRET_KEY` con:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Avvio

```bash
# Assicurati che search_cli sia compilato
make -C ../chess-engine/build search_cli

# Avvia il server
source venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Apri il browser su [http://localhost:8000](http://localhost:8000).

---

## Struttura del progetto

```
server.py           backend FastAPI (API REST + WebSocket)
requirements.txt    dipendenze Python
static/
  index.html        home page / lobby
  room.html         scacchiera di gioco
  login.html        pagina login
  register.html     pagina registrazione
  forgot.html       recupero password
  reset.html        reset password
  profile.html      profilo utente e storico partite
  img/              immagini statiche
logs/               log rotanti (chess.log, max 5x5 MB)
vibechess.db        database SQLite (utenti, partite)
```

---

## Architettura

```
  Browser
  +---------------------------+
  |  chessboard.js + chess.js |   drag-and-drop, validazione lato client
  |  HTML/CSS/JS              |   index, room, login, register, profile
  +-------------+-------------+
                | HTTP REST + WebSocket
  +-------------+-------------+
  |    server.py  (FastAPI)   |   gestione stanze, auth JWT, storico
  +------+----------+---------+
         |          |
  +------+---+  +---+----------+
  | SQLite   |  | search_cli   |   ../chess-engine/build/search_cli
  | (utenti, |  | (negamax,    |
  |  partite)|  |  depth 7-11) |
  +----------+  +--------------+
                |
  +-------------+-----------+
  |  OpenAI gpt-4o-mini     |   solo modalita' ChatGPT
  +-------------------------+
```

---

## API principali

### REST

| Metodo | Path | Descrizione |
|---|---|---|
| `POST` | `/api/rooms` | Crea una nuova stanza |
| `GET` | `/api/rooms` | Lista stanze attive |
| `POST` | `/api/auth/register` | Registrazione utente |
| `POST` | `/api/auth/login` | Login |
| `POST` | `/api/auth/logout` | Logout |
| `GET` | `/api/auth/me` | Utente corrente |
| `GET` | `/api/users/{alias}` | Profilo pubblico |
| `GET` | `/api/users/{alias}/games` | Storico partite |

### WebSocket

```
ws://localhost:8000/ws/{room_id}
```

Messaggi principali: `join`, `move`, `game_over`, `resign`.

---

## Log

I log vengono scritti in `logs/chess.log` con rotazione automatica (5 MB x 5 file).

```bash
tail -f logs/chess.log
```

Ogni mossa del motore registra: FEN, profondita', score, nodi, nps, tempo.
