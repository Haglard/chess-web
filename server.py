#!/usr/bin/env python3
"""
chess-web — server FastAPI per scacchi multiplayer via browser.
Dipendenze: fastapi, uvicorn[standard], chess, openai, python-dotenv,
            aiosqlite, passlib[bcrypt], python-jose[cryptography]
"""

import asyncio
import json
import logging
import logging.handlers
import os
import random
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import aiosqlite
import chess
import openai
from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
def _setup_logging() -> logging.Logger:
    BASE = Path(__file__).parent
    log_dir = BASE / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "chess.log"

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)-5s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # File rotante: max 5 MB × 5 backup = 25 MB totali
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    lg = logging.getLogger("chess")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(fh)
    lg.addHandler(ch)
    lg.propagate = False
    return lg

logger = _setup_logging()

def L(room_id: str, msg: str, level: str = "info") -> None:
    """Shortcut per log contestualizzato alla stanza."""
    getattr(logger, level)("[%s] %s", room_id, msg)

# ── Configurazione ───────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
STATIC_DIR      = BASE_DIR / "static"
ENGINE_PATH     = BASE_DIR.parent / "chess-engine" / "build" / "search_cli"
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
CHATGPT_MODEL   = "gpt-4o-mini"
DB_PATH         = BASE_DIR / "vibechess.db"

SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print(f"[WARN] SECRET_KEY non trovata in .env — uso temporanea: {SECRET_KEY}")

JWT_ALGORITHM  = "HS256"
JWT_EXPIRE_DAYS = 30
ALIAS_RE = re.compile(r'^[a-zA-Z0-9_-]{3,24}$')

pwd_ctx = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

app = FastAPI(title="Vibe Chess", version="1.1")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── DB inizializzazione ──────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                alias TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                bio TEXT DEFAULT '',
                avatar TEXT DEFAULT '♟',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS password_resets (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                used INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS games (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                room_type TEXT NOT NULL,
                white_user_id TEXT,
                black_user_id TEXT,
                white_alias TEXT NOT NULL,
                black_alias TEXT NOT NULL,
                result TEXT,
                reason TEXT,
                moves_count INTEGER DEFAULT 0,
                started_at REAL,
                ended_at REAL
            );
        """)
        await db.commit()

@app.on_event("startup")
async def startup():
    await init_db()

# ── Auth helpers ─────────────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    return pwd_ctx.hash(pw)

def verify_password(pw: str, hashed: str) -> bool:
    return pwd_ctx.verify(pw, hashed)

def create_jwt(user_id: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS)
    return jwt.encode({"sub": user_id, "exp": exp}, SECRET_KEY, algorithm=JWT_ALGORITHM)

def decode_jwt(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None

async def get_user_by_id(user_id: str) -> Optional[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_current_user(vc_token: Optional[str] = Cookie(default=None)) -> Optional[dict]:
    if not vc_token:
        return None
    user_id = decode_jwt(vc_token)
    if not user_id:
        return None
    return await get_user_by_id(user_id)

async def require_auth(vc_token: Optional[str] = Cookie(default=None)) -> dict:
    user = await get_current_user(vc_token)
    if not user:
        raise HTTPException(status_code=401, detail="Autenticazione richiesta")
    return user

# ── Stato in memoria ─────────────────────────────────────────────────────────
# rooms[room_id] = dict con tutto lo stato della partita
rooms: Dict[str, dict] = {}
# ws_pool[room_id] = lista di WebSocket connesse
ws_pool: Dict[str, List[WebSocket]] = {}
# ws_color[id(ws)] = "white" | "black" | "spectator"
ws_color: Dict[int, str] = {}

# ── Helpers ──────────────────────────────────────────────────────────────────
def room_info(r: dict) -> dict:
    """Sommario della stanza per la home page."""
    return {
        "id":         r["id"],
        "type":       r["type"],
        "status":     r["status"],
        "white":      r["white"]["nick"] if r["white"] else None,
        "black":      r["black"]["nick"] if r["black"] else None,
        "white_alias": r["white"]["nick"] if r["white"] and not r["white"].get("is_computer") else None,
        "black_alias": r["black"]["nick"] if r["black"] and not r["black"].get("is_computer") else None,
        "created_at": r["created_at"].isoformat(),
        "moves":      len(r["moves"]),
    }

def make_board(moves: list) -> chess.Board:
    b = chess.Board()
    for m in moves:
        b.push_uci(m)
    return b

def check_result(board: chess.Board, total_moves: int = 0) -> Optional[tuple]:
    """Ritorna (result_string, motivo) oppure None se la partita continua.
    Usa board.outcome(claim_draw=True) che copre TUTTI i casi FIDE:
      checkmate, stalemate, insufficient material,
      50-move rule (claim), 75-move rule (auto),
      threefold repetition (claim), fivefold repetition (auto).
    Aggiunge anche un limite di sicurezza a 400 mosse totali.
    """
    _reasons = {
        chess.Termination.CHECKMATE:            "scacco matto",
        chess.Termination.STALEMATE:            "stallo",
        chess.Termination.INSUFFICIENT_MATERIAL:"materiale insufficiente",
        chess.Termination.FIFTY_MOVES:          "regola delle 50 mosse",
        chess.Termination.SEVENTYFIVE_MOVES:    "regola delle 75 mosse",
        chess.Termination.THREEFOLD_REPETITION: "triplice ripetizione",
        chess.Termination.FIVEFOLD_REPETITION:  "quintuplice ripetizione",
    }
    outcome = board.outcome(claim_draw=True)
    if outcome is not None:
        reason = _reasons.get(outcome.termination, str(outcome.termination))
        return outcome.result(), reason
    # Limite di sicurezza: se la partita supera 400 mosse forziamo patta
    if total_moves >= 400:
        return "1/2-1/2", "limite massimo di mosse raggiunto"
    return None

async def broadcast(room_id: str, msg: dict, exclude: WebSocket = None):
    """Invia un messaggio JSON a tutti i WebSocket della stanza."""
    txt  = json.dumps(msg, ensure_ascii=False)
    dead = []
    for ws in list(ws_pool.get(room_id, [])):
        if ws is exclude:
            continue
        try:
            await ws.send_text(txt)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_pool[room_id].remove(ws)
        ws_color.pop(id(ws), None)

def _think_ms_to_depth(think_ms: int) -> int:
    s = think_ms / 1000
    if s <= 1:  return 7
    if s <= 4:  return 8
    if s <= 10: return 9
    if s <= 30: return 10
    return 11

_DEPTH_TIMEOUT = {7: 30, 8: 60, 9: 120, 10: 360, 11: 720}


# ── Salvataggio partita ──────────────────────────────────────────────────────
async def save_game(room_id: str, result: str, reason: str):
    room = rooms.get(room_id)
    if not room:
        return
    try:
        white = room.get("white") or {}
        black = room.get("black") or {}
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute(
                """INSERT OR IGNORE INTO games
                   (id, room_id, room_type, white_user_id, black_user_id,
                    white_alias, black_alias, result, reason, moves_count,
                    started_at, ended_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()),
                    room_id,
                    room.get("type", "unknown"),
                    white.get("user_id"),
                    black.get("user_id"),
                    white.get("nick", "—"),
                    black.get("nick", "—"),
                    result,
                    reason,
                    len(room.get("moves", [])),
                    room.get("game_started_at").timestamp() if room.get("game_started_at") else None,
                    time.time(),
                )
            )
            await db.commit()
        L(room_id, f"DB SAVE OK | {result} ({reason}) mosse={len(room.get('moves',[]))}")
    except Exception as e:
        L(room_id, f"DB SAVE ERROR: {e}", "error")

# ── Motore di scacchi (search_cli) ───────────────────────────────────────────
async def engine_move(moves: list, think_ms: int, room_id: str = "—") -> Optional[dict]:
    proc = None
    t_start = time.monotonic()
    try:
        board = make_board(moves)
        fen   = board.fen()
        depth = _think_ms_to_depth(think_ms)
        turn  = "w" if board.turn == chess.WHITE else "b"
        timeout = _DEPTH_TIMEOUT.get(depth, 360)

        L(room_id, f"ENGINE START | turn={turn} ply={len(moves)} depth={depth} "
                   f"timeout={timeout}s | FEN={fen}")

        proc = await asyncio.create_subprocess_exec(
            str(ENGINE_PATH),
            "--fen",     fen,
            "--depth",   str(depth),
            "--qsearch", "1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        best_move = None
        d = None; score_cp = None; nodes = None; nps = None; time_ms_val = None

        try:
            while True:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if not raw:
                    break
                line = raw.decode().strip()
                if line.startswith("info depth"):
                    parts = line.split()
                    def _get(key):
                        try: return parts[parts.index(key) + 1]
                        except (ValueError, IndexError): return None
                    if _get("depth"):   d        = int(_get("depth"))
                    if _get("cp"):      score_cp = int(_get("cp"))
                    if _get("nodes"):   nodes    = int(_get("nodes"))
                    if _get("nps"):     nps      = float(_get("nps"))
                    logger.debug("[%s] ENGINE INFO | depth=%s score=%s nodes=%s",
                                 room_id, d, score_cp, nodes)
                elif line.startswith("bestmove"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] != "(none)":
                        best_move = parts[1]
                    break
                elif "time (ms)" in line:
                    try: time_ms_val = float(line.split(":")[1].strip())
                    except (IndexError, ValueError): pass
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t_start
            L(room_id, f"ENGINE TIMEOUT dopo {elapsed:.1f}s | "
                       f"depth={depth} limit={timeout}s | FEN={fen}", "warning")

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()

        elapsed = time.monotonic() - t_start
        if best_move is None:
            L(room_id, f"ENGINE NONE | nessun bestmove dopo {elapsed:.1f}s | "
                       f"depth={depth} | FEN={fen}", "warning")
            return None

        score_str = f"{score_cp/100:+.2f}" if score_cp is not None else "n/a"
        nodes_str = f"{nodes/1e6:.2f}M" if nodes and nodes >= 1e6 else str(nodes)
        L(room_id, f"ENGINE OK | best={best_move} depth={d} score={score_str} "
                   f"nodes={nodes_str} elapsed={elapsed:.1f}s | FEN={fen}")

        return {
            "move":     best_move,
            "depth":    d,
            "score_cp": score_cp,
            "nodes":    nodes,
            "nps":      nps,
            "time_ms":  time_ms_val,
        }

    except Exception as e:
        elapsed = time.monotonic() - t_start
        L(room_id, f"ENGINE EXCEPTION dopo {elapsed:.1f}s: {e}", "error")
        if proc:
            try: proc.kill()
            except Exception: pass
        return None

# ── ChatGPT ──────────────────────────────────────────────────────────────────
async def chatgpt_move(moves: list, room_id: str = "—") -> Optional[dict]:
    if not OPENAI_API_KEY:
        L(room_id, "CHATGPT SKIP | OPENAI_API_KEY non configurata", "warning")
        return None

    board     = make_board(moves)
    legal_uci = [m.uci() for m in board.legal_moves]
    turn      = "w" if board.turn == chess.WHITE else "b"
    if not legal_uci:
        L(room_id, f"CHATGPT SKIP | nessuna mossa legale | FEN={board.fen()}", "warning")
        return None

    L(room_id, f"CHATGPT START | turn={turn} ply={len(moves)} "
               f"legal={len(legal_uci)} | FEN={board.fen()}")

    san_parts = []
    tmp = chess.Board()
    for uci in moves:
        m = chess.Move.from_uci(uci)
        san_parts.append(tmp.san(m))
        tmp.push(m)

    history_str = ""
    if san_parts:
        chunks = []
        for i in range(0, len(san_parts), 2):
            num = i // 2 + 1
            if i + 1 < len(san_parts):
                chunks.append(f"{num}. {san_parts[i]} {san_parts[i+1]}")
            else:
                chunks.append(f"{num}. {san_parts[i]}")
        history_str = " ".join(chunks)

    color_name   = "Bianco" if board.turn == chess.WHITE else "Nero"
    legal_str    = ", ".join(legal_uci)
    history_line = ("Mosse della partita: " + history_str) if history_str else "Siamo all'inizio della partita."

    base_prompt = f"""Stai giocando a scacchi come {color_name}.

Posizione attuale (FEN): {board.fen()}
{history_line}

Mosse legali disponibili (notazione UCI): {legal_str}

Scegli la mossa migliore. Rispondi ESCLUSIVAMENTE con un oggetto JSON valido nel formato:
{{"move": "<uci>", "reasoning": "<breve spiegazione in italiano, max 2 frasi>"}}

La mossa DEVE essere esattamente una di quelle nell'elenco delle mosse legali."""

    prompt = base_prompt
    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

    t_start = time.monotonic()
    for attempt in range(3):
        L(room_id, f"CHATGPT ATTEMPT {attempt+1}/3")
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=CHATGPT_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=250,
                ),
                timeout=30.0,
            )
            raw_content = resp.choices[0].message.content.strip()
            logger.debug("[%s] CHATGPT RAW | %s", room_id, raw_content[:200])

            content = raw_content
            if "```" in content:
                parts = content.split("```")
                for part in parts:
                    part = part.strip()
                    if part.startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        content = part
                        break

            data      = json.loads(content)
            uci       = str(data.get("move", "")).strip()
            reasoning = str(data.get("reasoning", "")).strip()

            if uci in legal_uci:
                elapsed = time.monotonic() - t_start
                L(room_id, f"CHATGPT OK | move={uci} elapsed={elapsed:.1f}s | {reasoning[:80]}")
                return {"move": uci, "reasoning": reasoning}

            L(room_id, f"CHATGPT MOSSA ILLEGALE '{uci}' (tentativo {attempt+1})", "warning")
            prompt = base_prompt + (
                f"\n\nATTENZIONE: '{uci}' non è una mossa valida. "
                f"Devi scegliere SOLO da questa lista: {legal_str}"
            )

        except asyncio.TimeoutError:
            L(room_id, f"CHATGPT TIMEOUT tentativo {attempt+1}", "warning")
        except json.JSONDecodeError as e:
            L(room_id, f"CHATGPT JSON NON VALIDO tentativo {attempt+1}: {e}", "warning")
            prompt = base_prompt + "\n\nRispondi SOLO con JSON valido, nessun testo aggiuntivo."
        except Exception as e:
            L(room_id, f"CHATGPT API ERROR tentativo {attempt+1}: {e}", "error")

    elapsed = time.monotonic() - t_start
    fallback_uci = random.choice(legal_uci)
    L(room_id, f"CHATGPT FALLBACK casuale={fallback_uci} dopo {elapsed:.1f}s "
               f"| FEN={board.fen()}", "warning")
    return {"move": fallback_uci, "reasoning": "⚠ Mossa casuale (ChatGPT non ha risposto correttamente)"}

# ── Trigger mosse AI ─────────────────────────────────────────────────────────
async def trigger_computer_move(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    if room["status"] != "active":
        L(room_id, f"ENGINE SKIP | status={room['status']}")
        return
    if room.get("computer_thinking"):
        L(room_id, "ENGINE SKIP | computer_thinking=True (già in corso)")
        return

    board         = make_board(room["moves"])
    turn_color    = "w" if board.turn == chess.WHITE else "b"
    comp_is_white = room["white"] and room["white"].get("is_computer") and not room["white"].get("is_chatgpt")
    comp_is_black = room["black"] and room["black"].get("is_computer") and not room["black"].get("is_chatgpt")
    if board.turn == chess.WHITE and not comp_is_white:
        L(room_id, f"ENGINE SKIP | turno={turn_color} ma engine non gioca bianco")
        return
    if board.turn == chess.BLACK and not comp_is_black:
        L(room_id, f"ENGINE SKIP | turno={turn_color} ma engine non gioca nero")
        return

    fen_before = board.fen()
    ply        = len(room["moves"])
    L(room_id, f"ENGINE TRIGGER | turn={turn_color} ply={ply} "
               f"thinking→True | FEN={fen_before}")
    room["computer_thinking"] = True
    try:
        result_dict = await engine_move(room["moves"], room["think_ms"], room_id)

        # ── Fallback casuale se il motore non risponde ──────────────────────
        if not result_dict:
            legal = list(board.legal_moves)
            if not legal:
                L(room_id, "ENGINE NONE + nessuna mossa legale → partita bloccata", "error")
                return
            fallback = random.choice(legal)
            uci = fallback.uci()
            L(room_id, f"ENGINE FALLBACK casuale={uci} (engine=None) | FEN={fen_before}", "warning")
            result_dict = {"move": uci, "depth": None, "score_cp": None,
                           "nodes": None, "nps": None, "time_ms": None}

        uci  = result_dict["move"]
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            # Mossa illegale restituita dal motore → fallback casuale
            legal = list(board.legal_moves)
            L(room_id, f"ENGINE MOSSA ILLEGALE '{uci}' → fallback casuale | FEN={fen_before}", "warning")
            if not legal:
                return
            move = random.choice(legal)
            uci  = move.uci()
            result_dict = {**result_dict, "move": uci}

        board.push(move)
        room["moves"].append(uci)
        room["move_timestamps"].append(datetime.now())
        room["last_engine_stats"] = {k: v for k, v in result_dict.items() if k != "move"}
        room["fen"] = board.fen()

        L(room_id, f"ENGINE MOSSA APPLICATA | {uci} | nuovo FEN={room['fen']}")

        res = check_result(board, len(room["moves"]))
        if res:
            result_str, reason = res
            room["status"] = "finished"
            room["result"] = result_str
            L(room_id, f"ENGINE GAME OVER | {result_str} ({reason})")
            await broadcast(room_id, {
                "type":         "game_over",
                "uci":          uci,
                "fen":          room["fen"],
                "result":       result_str,
                "reason":       reason,
                "moves":        room["moves"],
                "move_ts":      datetime.now().timestamp(),
                "engine_stats": room["last_engine_stats"],
            })
            asyncio.create_task(save_game(room_id, result_str, reason))
        else:
            turn_next = "w" if board.turn == chess.WHITE else "b"
            await broadcast(room_id, {
                "type":         "move",
                "uci":          uci,
                "fen":          room["fen"],
                "turn":         turn_next,
                "moves":        room["moves"],
                "move_ts":      datetime.now().timestamp(),
                "engine_stats": room["last_engine_stats"],
            })
            if room["type"] in ("CvChatGPT", "CvC"):
                L(room_id, f"ENGINE → schedulo trigger_auto_move (prossimo turn={turn_next})")
                asyncio.create_task(trigger_auto_move(room_id))
    finally:
        room["computer_thinking"] = False
        L(room_id, f"ENGINE thinking→False | ply={len(room.get('moves', []))}")


async def trigger_chatgpt_move(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    if room["status"] != "active":
        L(room_id, f"CHATGPT SKIP | status={room['status']}")
        return
    if room.get("computer_thinking"):
        L(room_id, "CHATGPT SKIP | computer_thinking=True (già in corso)")
        return

    board         = make_board(room["moves"])
    turn_color    = "w" if board.turn == chess.WHITE else "b"
    gpt_is_white  = room["white"] and room["white"].get("is_chatgpt")
    gpt_is_black  = room["black"] and room["black"].get("is_chatgpt")
    if board.turn == chess.WHITE and not gpt_is_white:
        L(room_id, f"CHATGPT SKIP | turno={turn_color} ma chatgpt non gioca bianco")
        return
    if board.turn == chess.BLACK and not gpt_is_black:
        L(room_id, f"CHATGPT SKIP | turno={turn_color} ma chatgpt non gioca nero")
        return

    fen_before = board.fen()
    ply        = len(room["moves"])
    L(room_id, f"CHATGPT TRIGGER | turn={turn_color} ply={ply} "
               f"thinking→True | FEN={fen_before}")
    room["computer_thinking"] = True
    try:
        result_dict = await chatgpt_move(room["moves"], room_id)

        # ── Fallback se chatgpt_move ritorna None (non dovrebbe succedere) ──
        if not result_dict:
            legal = list(board.legal_moves)
            if not legal:
                L(room_id, "CHATGPT NONE + nessuna mossa legale → partita bloccata", "error")
                return
            uci = random.choice(legal).uci()
            L(room_id, f"CHATGPT FALLBACK casuale={uci} (result=None) | FEN={fen_before}", "warning")
            result_dict = {"move": uci, "reasoning": "⚠ Fallback interno"}

        uci  = result_dict["move"]
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            legal = list(board.legal_moves)
            L(room_id, f"CHATGPT MOSSA ILLEGALE '{uci}' → fallback casuale | FEN={fen_before}", "warning")
            if not legal:
                return
            move = random.choice(legal)
            uci  = move.uci()
            result_dict = {**result_dict, "move": uci}

        board.push(move)
        room["moves"].append(uci)
        room["move_timestamps"].append(datetime.now())
        room["last_engine_stats"] = {
            "chatgpt_reasoning": result_dict.get("reasoning", ""),
        }
        room["fen"] = board.fen()

        L(room_id, f"CHATGPT MOSSA APPLICATA | {uci} | nuovo FEN={room['fen']}")

        res = check_result(board, len(room["moves"]))
        if res:
            result_str, reason = res
            room["status"] = "finished"
            room["result"] = result_str
            L(room_id, f"CHATGPT GAME OVER | {result_str} ({reason})")
            await broadcast(room_id, {
                "type":         "game_over",
                "uci":          uci,
                "fen":          room["fen"],
                "result":       result_str,
                "reason":       reason,
                "moves":        room["moves"],
                "move_ts":      datetime.now().timestamp(),
                "engine_stats": room["last_engine_stats"],
            })
            asyncio.create_task(save_game(room_id, result_str, reason))
        else:
            turn_next = "w" if board.turn == chess.WHITE else "b"
            await broadcast(room_id, {
                "type":         "move",
                "uci":          uci,
                "fen":          room["fen"],
                "turn":         turn_next,
                "moves":        room["moves"],
                "move_ts":      datetime.now().timestamp(),
                "engine_stats": room["last_engine_stats"],
            })
            if room["type"] in ("CvChatGPT", "CvC"):
                L(room_id, f"CHATGPT → schedulo trigger_auto_move (prossimo turn={turn_next})")
                asyncio.create_task(trigger_auto_move(room_id))
    finally:
        room["computer_thinking"] = False
        L(room_id, f"CHATGPT thinking→False | ply={len(room.get('moves', []))}")


async def trigger_auto_move(room_id: str):
    room = rooms.get(room_id)
    if not room or room["status"] != "active":
        L(room_id, f"AUTO SKIP | room assente o status={room.get('status') if room else 'N/A'}")
        return
    await asyncio.sleep(0.5)
    board = make_board(room["moves"])
    turn_color = "w" if board.turn == chess.WHITE else "b"
    slot  = room["white"] if board.turn == chess.WHITE else room["black"]
    if not slot:
        L(room_id, f"AUTO SKIP | slot {turn_color} è None", "warning")
        return
    if slot.get("is_chatgpt"):
        L(room_id, f"AUTO → trigger_chatgpt_move (turn={turn_color} ply={len(room['moves'])})")
        asyncio.create_task(trigger_chatgpt_move(room_id))
    elif slot.get("is_computer"):
        L(room_id, f"AUTO → trigger_computer_move (turn={turn_color} ply={len(room['moves'])})")
        asyncio.create_task(trigger_computer_move(room_id))

# ── Auth API ─────────────────────────────────────────────────────────────────

class RegisterBody(BaseModel):
    email: str
    alias: str
    password: str
    bio: Optional[str] = ""

@app.post("/api/auth/register")
async def register(body: RegisterBody):
    # Validazioni
    if not body.email or "@" not in body.email:
        raise HTTPException(400, "Email non valida")
    if not ALIAS_RE.match(body.alias):
        raise HTTPException(400, "Alias non valido: 3-24 caratteri alfanumerici, - e _")
    if len(body.password) < 8:
        raise HTTPException(400, "Password troppo corta (min 8 caratteri)")

    async with aiosqlite.connect(str(DB_PATH)) as db:
        async with db.execute("SELECT id FROM users WHERE email=?", (body.email.lower(),)) as cur:
            if await cur.fetchone():
                raise HTTPException(409, "Email già registrata")
        async with db.execute("SELECT id FROM users WHERE alias=?", (body.alias,)) as cur:
            if await cur.fetchone():
                raise HTTPException(409, "Alias già in uso")

        user_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO users (id, email, alias, password_hash, bio, avatar, created_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, body.email.lower(), body.alias, hash_password(body.password),
             body.bio or "", "♟", time.time())
        )
        await db.commit()

    token = create_jwt(user_id)
    resp = JSONResponse({"ok": True, "alias": body.alias})
    resp.set_cookie(
        "vc_token", token,
        max_age=60 * 60 * 24 * JWT_EXPIRE_DAYS,
        httponly=True, samesite="lax"
    )
    return resp


class LoginBody(BaseModel):
    email: str
    password: str

@app.post("/api/auth/login")
async def login(body: LoginBody):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE email=?", (body.email.lower(),)) as cur:
            row = await cur.fetchone()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Credenziali non valide")

    token = create_jwt(row["id"])
    resp = JSONResponse({"ok": True, "alias": row["alias"]})
    resp.set_cookie(
        "vc_token", token,
        max_age=60 * 60 * 24 * JWT_EXPIRE_DAYS,
        httponly=True, samesite="lax"
    )
    return resp


@app.post("/api/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("vc_token")
    return resp


@app.get("/api/auth/me")
async def auth_me(vc_token: Optional[str] = Cookie(default=None)):
    user = await get_current_user(vc_token)
    if not user:
        return JSONResponse({"logged_in": False})
    return JSONResponse({
        "logged_in": True,
        "id":     user["id"],
        "email":  user["email"],
        "alias":  user["alias"],
        "bio":    user["bio"],
        "avatar": user["avatar"],
    })


class ForgotBody(BaseModel):
    email: str

@app.post("/api/auth/forgot-password")
async def forgot_password(body: ForgotBody):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM users WHERE email=?", (body.email.lower(),)) as cur:
            row = await cur.fetchone()
        if row:
            token = str(uuid.uuid4())
            expires = time.time() + 7200  # 2h
            await db.execute(
                "INSERT INTO password_resets (token, user_id, expires_at) VALUES (?,?,?)",
                (token, row["id"], expires)
            )
            await db.commit()
            print(f"[PASSWORD RESET] Link: http://localhost:8000/reset-password?token={token}")
    return JSONResponse({"ok": True, "msg": "Se l'email esiste riceverai un link"})


class ResetBody(BaseModel):
    token: str
    password: str

@app.post("/api/auth/reset-password")
async def reset_password(body: ResetBody):
    if len(body.password) < 8:
        raise HTTPException(400, "Password troppo corta (min 8 caratteri)")
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM password_resets WHERE token=? AND used=0 AND expires_at>?",
            (body.token, time.time())
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise HTTPException(400, "Token non valido o scaduto")
        await db.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (hash_password(body.password), row["user_id"])
        )
        await db.execute("UPDATE password_resets SET used=1 WHERE token=?", (body.token,))
        await db.commit()
    return JSONResponse({"ok": True})


class UpdateMeBody(BaseModel):
    bio: Optional[str] = None
    avatar: Optional[str] = None

@app.put("/api/users/me")
async def update_me(body: UpdateMeBody, vc_token: Optional[str] = Cookie(default=None)):
    user = await get_current_user(vc_token)
    if not user:
        raise HTTPException(401, "Autenticazione richiesta")
    fields = []
    vals = []
    if body.bio is not None:
        fields.append("bio=?"); vals.append(body.bio)
    if body.avatar is not None:
        fields.append("avatar=?"); vals.append(body.avatar)
    if not fields:
        return JSONResponse({"ok": True})
    vals.append(user["id"])
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", vals)
        await db.commit()
    return JSONResponse({"ok": True})


@app.get("/api/users/{alias}")
async def get_user_profile(alias: str):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, alias, bio, avatar, created_at FROM users WHERE alias=?", (alias,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "Utente non trovato")
    return JSONResponse(dict(row))


@app.get("/api/users/{alias}/games")
async def get_user_games(alias: str):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM users WHERE alias=?", (alias,)
        ) as cur:
            user_row = await cur.fetchone()
        if not user_row:
            raise HTTPException(404, "Utente non trovato")
        user_id = user_row["id"]
        async with db.execute(
            """SELECT * FROM games
               WHERE white_user_id=? OR black_user_id=?
               ORDER BY ended_at DESC LIMIT 100""",
            (user_id, user_id)
        ) as cur:
            rows = await cur.fetchall()
    return JSONResponse([dict(r) for r in rows])

# ── Pagine HTML ───────────────────────────────────────────────────────────────
@app.get("/")
async def home():
    return HTMLResponse((STATIC_DIR / "index.html").read_text())

@app.get("/room/{room_id}")
async def room_page(room_id: str):
    if room_id not in rooms:
        raise HTTPException(404, "Stanza non trovata")
    return HTMLResponse((STATIC_DIR / "room.html").read_text())

@app.get("/login")
async def login_page():
    return HTMLResponse((STATIC_DIR / "login.html").read_text())

@app.get("/register")
async def register_page():
    return HTMLResponse((STATIC_DIR / "register.html").read_text())

@app.get("/forgot-password")
async def forgot_page():
    return HTMLResponse((STATIC_DIR / "forgot.html").read_text())

@app.get("/reset-password")
async def reset_page():
    return HTMLResponse((STATIC_DIR / "reset.html").read_text())

@app.get("/profile/{alias}")
async def profile_page(alias: str):
    return HTMLResponse((STATIC_DIR / "profile.html").read_text())

# ── Room API ──────────────────────────────────────────────────────────────────
@app.get("/api/rooms")
async def list_rooms():
    return JSONResponse([
        room_info(r) for r in rooms.values()
        if r["status"] != "finished"
    ])

class CreateRoomBody(BaseModel):
    type:      str
    nickname:  str
    color:     str           # "white" | "black" | "random"
    think_sec: Optional[int] = 3

@app.post("/api/rooms")
async def create_room(body: CreateRoomBody, vc_token: Optional[str] = Cookie(default=None)):
    user = await get_current_user(vc_token)
    if not user:
        raise HTTPException(401, "Autenticazione richiesta")

    color = body.color
    if color == "random":
        color = random.choice(["white", "black"])

    nick     = user["alias"]
    room_id  = uuid.uuid4().hex[:6].upper()
    think_ms = max(500, (body.think_sec or 3) * 1000)

    def make_user_slot(nick, user_id):
        return {"nick": nick, "is_computer": False, "is_chatgpt": False, "user_id": user_id}

    def make_ai_slot(nick, is_chatgpt=False):
        return {"nick": nick, "is_computer": True, "is_chatgpt": is_chatgpt, "user_id": None}

    if body.type == "UvsU":
        white_slot = make_user_slot(nick, user["id"]) if color == "white" else None
        black_slot = make_user_slot(nick, user["id"]) if color == "black" else None
        status     = "waiting"
        return_color = color

    elif body.type == "UvsC":
        comp_nick = f"Engine (d{_think_ms_to_depth(think_ms)})"
        if color == "white":
            white_slot = make_user_slot(nick, user["id"])
            black_slot = make_ai_slot(comp_nick)
        else:
            white_slot = make_ai_slot(comp_nick)
            black_slot = make_user_slot(nick, user["id"])
        status       = "active"
        return_color = color

    elif body.type == "UvChatGPT":
        gpt_nick = "ChatGPT 🤖"
        if color == "white":
            white_slot = make_user_slot(nick, user["id"])
            black_slot = make_ai_slot(gpt_nick, is_chatgpt=True)
        else:
            white_slot = make_ai_slot(gpt_nick, is_chatgpt=True)
            black_slot = make_user_slot(nick, user["id"])
        status       = "active"
        return_color = color

    elif body.type == "CvChatGPT":
        eng_nick = f"Engine (d{_think_ms_to_depth(think_ms)})"
        gpt_nick = "ChatGPT 🤖"
        if color == "white":
            white_slot = make_ai_slot(eng_nick)
            black_slot = make_ai_slot(gpt_nick, is_chatgpt=True)
        else:
            white_slot = make_ai_slot(gpt_nick, is_chatgpt=True)
            black_slot = make_ai_slot(eng_nick)
        status       = "active"
        return_color = "spectator"

    elif body.type == "CvC":
        d = _think_ms_to_depth(think_ms)
        white_slot = make_ai_slot(f"Engine-W (d{d})")
        black_slot = make_ai_slot(f"Engine-B (d{d})")
        status       = "active"
        return_color = "spectator"

    else:
        raise HTTPException(400, f"Tipo non supportato: {body.type}")

    board = chess.Board()
    rooms[room_id] = {
        "id":                room_id,
        "type":              body.type,
        "status":            status,
        "created_at":        datetime.now(),
        "white":             white_slot,
        "black":             black_slot,
        "fen":               board.fen(),
        "moves":             [],
        "think_ms":          think_ms,
        "result":            None,
        "computer_thinking": False,
        "game_started_at":   None,
        "move_timestamps":   [],
        "last_engine_stats": None,
    }
    ws_pool[room_id] = []

    if status == "active":
        rooms[room_id]["game_started_at"] = datetime.now()

    w_nick = white_slot["nick"] if white_slot else "—"
    b_nick = black_slot["nick"] if black_slot else "—"
    logger.info("[%s] ROOM CREATED | type=%s white=%s black=%s status=%s depth=%s",
                room_id, body.type, w_nick, b_nick, status,
                _think_ms_to_depth(think_ms))

    return JSONResponse({"room_id": room_id, "color": return_color})

class JoinBody(BaseModel):
    nickname: str

@app.post("/api/rooms/{room_id}/join")
async def join_room(room_id: str, body: JoinBody, vc_token: Optional[str] = Cookie(default=None)):
    user = await get_current_user(vc_token)
    if not user:
        raise HTTPException(401, "Autenticazione richiesta")

    room = rooms.get(room_id)
    if not room:
        raise HTTPException(404, "Stanza non trovata")
    if room["type"] != "UvsU" or room["status"] != "waiting":
        raise HTTPException(400, "Stanza non disponibile")

    nick = user["alias"]
    if room["white"] is None:
        room["white"] = {"nick": nick, "is_computer": False, "is_chatgpt": False, "user_id": user["id"]}
        color = "white"
    elif room["black"] is None:
        room["black"] = {"nick": nick, "is_computer": False, "is_chatgpt": False, "user_id": user["id"]}
        color = "black"
    else:
        raise HTTPException(400, "Stanza piena")

    room["status"] = "active"
    room["game_started_at"] = datetime.now()

    await broadcast(room_id, {
        "type":            "room_full",
        "white":           room["white"]["nick"],
        "black":           room["black"]["nick"],
        "fen":             room["fen"],
        "turn":            "w",
        "moves":           [],
        "game_started_at": room["game_started_at"].timestamp(),
    })

    return JSONResponse({"color": color, "room_id": room_id})

# ── WebSocket ────────────────────────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(websocket: WebSocket, room_id: str, color: str = "spectator"):
    room = rooms.get(room_id)
    if not room:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    ws_pool.setdefault(room_id, []).append(websocket)
    ws_color[id(websocket)] = color

    board = make_board(room["moves"])
    turn  = "w" if board.turn == chess.WHITE else "b"
    await websocket.send_text(json.dumps({
        "type":             "state",
        "fen":              room["fen"],
        "turn":             turn,
        "moves":            room["moves"],
        "status":           room["status"],
        "white":            room["white"]["nick"] if room["white"] else None,
        "black":            room["black"]["nick"] if room["black"] else None,
        "result":           room["result"],
        "game_started_at":  room["game_started_at"].timestamp() if room["game_started_at"] else None,
        "move_timestamps":  [t.timestamp() for t in room["move_timestamps"]],
        "last_engine_stats": room["last_engine_stats"],
        "room_type":        room["type"],
    }))

    if room["status"] == "active" and not room["moves"]:
        rtype  = room["type"]
        w_slot = room["white"]
        if rtype in ("UvsC", "CvChatGPT", "CvC"):
            if w_slot and w_slot.get("is_computer") and not w_slot.get("is_chatgpt"):
                asyncio.create_task(trigger_computer_move(room_id))
            elif w_slot and w_slot.get("is_chatgpt"):
                asyncio.create_task(trigger_chatgpt_move(room_id))
        elif rtype == "UvChatGPT":
            if w_slot and w_slot.get("is_chatgpt"):
                asyncio.create_task(trigger_chatgpt_move(room_id))

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            t   = msg.get("type")

            if t == "move":
                await handle_move(room_id, color, msg.get("uci", ""), websocket)
            elif t == "resign":
                await handle_resign(room_id, color)

    except WebSocketDisconnect:
        pass
    finally:
        pool = ws_pool.get(room_id, [])
        if websocket in pool:
            pool.remove(websocket)
        ws_color.pop(id(websocket), None)

async def handle_move(room_id: str, color: str, uci: str, ws: WebSocket):
    room = rooms.get(room_id)
    if not room or room["status"] != "active":
        await ws.send_text(json.dumps({"type": "error", "msg": "Partita non attiva"}))
        return

    if room["type"] in ("CvChatGPT", "CvC"):
        await ws.send_text(json.dumps({"type": "error", "msg": "Partita automatica, nessun input umano"}))
        return

    board          = make_board(room["moves"])
    expected_color = "white" if board.turn == chess.WHITE else "black"
    if color != expected_color:
        await ws.send_text(json.dumps({"type": "error", "msg": "Non è il tuo turno"}))
        return

    try:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError()
        board.push(move)
    except Exception:
        await ws.send_text(json.dumps({"type": "error", "msg": f"Mossa non valida: {uci}"}))
        return

    room["moves"].append(uci)
    room["move_timestamps"].append(datetime.now())
    room["fen"] = board.fen()

    res = check_result(board, len(room["moves"]))
    if res:
        result_str, reason = res
        room["status"] = "finished"
        room["result"] = result_str
        await broadcast(room_id, {
            "type":    "game_over",
            "uci":     uci,
            "fen":     room["fen"],
            "result":  result_str,
            "reason":  reason,
            "moves":   room["moves"],
            "move_ts": datetime.now().timestamp(),
        })
        asyncio.create_task(save_game(room_id, result_str, reason))
    else:
        turn = "w" if board.turn == chess.WHITE else "b"
        await broadcast(room_id, {
            "type":    "move",
            "uci":     uci,
            "fen":     room["fen"],
            "turn":    turn,
            "moves":   room["moves"],
            "move_ts": datetime.now().timestamp(),
        })
        if room["type"] == "UvsC":
            asyncio.create_task(trigger_computer_move(room_id))
        elif room["type"] == "UvChatGPT":
            asyncio.create_task(trigger_chatgpt_move(room_id))

async def handle_resign(room_id: str, color: str):
    room = rooms.get(room_id)
    if not room or room["status"] != "active":
        return
    result = "0-1" if color == "white" else "1-0"
    reason = ("Il bianco" if color == "white" else "Il nero") + " ha abbandonato"
    room["status"] = "finished"
    room["result"] = result
    await broadcast(room_id, {
        "type":   "game_over",
        "result": result,
        "reason": reason,
        "fen":    room["fen"],
        "moves":  room["moves"],
    })
    asyncio.create_task(save_game(room_id, result, reason))

# ── Avvio ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
