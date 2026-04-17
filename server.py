#!/usr/bin/env python3
"""
chess-web — server FastAPI per scacchi multiplayer via browser.
Dipendenze: fastapi, uvicorn[standard], chess, openai, python-dotenv
"""

import asyncio
import json
import os
import random
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import chess
import openai
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

# ── Configurazione ───────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
STATIC_DIR      = BASE_DIR / "static"
ENGINE_PATH     = BASE_DIR.parent / "chess-engine" / "build" / "search_cli"
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
CHATGPT_MODEL   = "gpt-4o-mini"

app = FastAPI(title="Vibe Chess", version="1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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
        "created_at": r["created_at"].isoformat(),
        "moves":      len(r["moves"]),
    }

def make_board(moves: list) -> chess.Board:
    b = chess.Board()
    for m in moves:
        b.push_uci(m)
    return b

def check_result(board: chess.Board) -> Optional[tuple]:
    """Ritorna (result_string, motivo) oppure None se la partita continua."""
    if board.is_checkmate():
        winner = "0-1" if board.turn == chess.WHITE else "1-0"
        return winner, "scacco matto"
    if board.is_stalemate():
        return "1/2-1/2", "stallo"
    if board.is_insufficient_material():
        return "1/2-1/2", "materiale insufficiente"
    if board.is_fifty_moves():
        return "1/2-1/2", "regola delle 50 mosse"
    if board.is_repetition(3):
        return "1/2-1/2", "triplice ripetizione"
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
    """
    Mappa il think time scelto dall'utente a una profondità di ricerca fissa.
    Benchmark su posizione tipica (opening):
      depth 7  ≈  140ms   depth 8  ≈  470ms   depth 9  ≈  2.4s
      depth 10 ≈  12s     depth 11 ≈  60s
    """
    s = think_ms / 1000
    if s <= 1:  return 7
    if s <= 4:  return 8
    if s <= 10: return 9
    if s <= 30: return 10
    return 11

# ── Motore di scacchi (search_cli) ───────────────────────────────────────────
async def engine_move(moves: list, think_ms: int) -> Optional[dict]:
    """
    Chiede a search_cli la mossa migliore con una profondità fissa derivata
    da think_ms.  Restituisce dict con move, depth, score_cp, nodes, nps, time_ms
    oppure None.
    """
    proc = None
    try:
        board = make_board(moves)
        fen   = board.fen()
        depth = _think_ms_to_depth(think_ms)

        proc = await asyncio.create_subprocess_exec(
            str(ENGINE_PATH),
            "--fen",     fen,
            "--depth",   str(depth),
            "--qsearch", "1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        timeout = max(think_ms / 1000.0 * 4, 90.0)
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
                elif line.startswith("bestmove"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] != "(none)":
                        best_move = parts[1]
                    break
                elif "time (ms)" in line:
                    try: time_ms_val = float(line.split(":")[1].strip())
                    except (IndexError, ValueError): pass
        except asyncio.TimeoutError:
            print(f"[engine] timeout (depth={depth})")

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()

        if best_move is None:
            return None

        return {
            "move":     best_move,
            "depth":    d,
            "score_cp": score_cp,
            "nodes":    nodes,
            "nps":      nps,
            "time_ms":  time_ms_val,
        }

    except Exception as e:
        print(f"[engine] errore: {e}")
        if proc:
            try: proc.kill()
            except Exception: pass
        return None

# ── ChatGPT ──────────────────────────────────────────────────────────────────
async def chatgpt_move(moves: list) -> Optional[dict]:
    """
    Chiede a gpt-4o-mini la mossa migliore.
    Ritorna {"move": uci, "reasoning": testo} oppure None se tutto fallisce.
    Retry fino a 3 volte; fallback su mossa casuale legale.
    """
    if not OPENAI_API_KEY:
        print("[chatgpt] OPENAI_API_KEY non configurata")
        return None

    board     = make_board(moves)
    legal_uci = [m.uci() for m in board.legal_moves]
    if not legal_uci:
        return None

    # Costruisce la storia delle mosse in notazione SAN leggibile
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

    for attempt in range(3):
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

            # Pulizia markdown code block se presente
            content = raw_content
            if "```" in content:
                parts = content.split("```")
                # prende il blocco dentro i backtick
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
                print(f"[chatgpt] mossa scelta: {uci} | {reasoning}")
                return {"move": uci, "reasoning": reasoning}

            print(f"[chatgpt] mossa '{uci}' non legale (tentativo {attempt+1})")
            prompt = base_prompt + (
                f"\n\nATTENZIONE: '{uci}' non è una mossa valida. "
                f"Devi scegliere SOLO da questa lista: {legal_str}"
            )

        except asyncio.TimeoutError:
            print(f"[chatgpt] timeout (tentativo {attempt+1})")
        except json.JSONDecodeError as e:
            print(f"[chatgpt] JSON non valido (tentativo {attempt+1}): {e}")
            prompt = base_prompt + "\n\nRispondi SOLO con JSON valido, nessun testo aggiuntivo."
        except Exception as e:
            print(f"[chatgpt] errore API (tentativo {attempt+1}): {e}")

    # Fallback: mossa casuale
    print("[chatgpt] fallback a mossa casuale")
    fallback_uci = random.choice(legal_uci)
    return {"move": fallback_uci, "reasoning": "⚠ Mossa casuale (ChatGPT non ha risposto correttamente)"}

# ── Trigger mosse AI ─────────────────────────────────────────────────────────
async def trigger_computer_move(room_id: str):
    """Calcola e applica la mossa del motore per la stanza."""
    room = rooms.get(room_id)
    if not room or room["status"] != "active" or room.get("computer_thinking"):
        return

    board         = make_board(room["moves"])
    comp_is_white = room["white"] and room["white"].get("is_computer") and not room["white"].get("is_chatgpt")
    comp_is_black = room["black"] and room["black"].get("is_computer") and not room["black"].get("is_chatgpt")
    if board.turn == chess.WHITE and not comp_is_white:
        return
    if board.turn == chess.BLACK and not comp_is_black:
        return

    room["computer_thinking"] = True
    try:
        result_dict = await engine_move(room["moves"], room["think_ms"])
        if not result_dict:
            return

        uci  = result_dict["move"]
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            print(f"[engine] mossa illegale ricevuta: {uci}")
            return

        board.push(move)
        room["moves"].append(uci)
        room["move_timestamps"].append(datetime.now())
        room["last_engine_stats"] = {k: v for k, v in result_dict.items() if k != "move"}
        room["fen"] = board.fen()

        res = check_result(board)
        if res:
            result_str, reason = res
            room["status"] = "finished"
            room["result"] = result_str
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
        else:
            turn = "w" if board.turn == chess.WHITE else "b"
            await broadcast(room_id, {
                "type":         "move",
                "uci":          uci,
                "fen":          room["fen"],
                "turn":         turn,
                "moves":        room["moves"],
                "move_ts":      datetime.now().timestamp(),
                "engine_stats": room["last_engine_stats"],
            })
            # Loop automatico per partite AI vs AI
            if room["type"] in ("CvChatGPT", "CvC"):
                asyncio.create_task(trigger_auto_move(room_id))
    finally:
        room["computer_thinking"] = False


async def trigger_chatgpt_move(room_id: str):
    """Calcola e applica la mossa di ChatGPT per la stanza."""
    room = rooms.get(room_id)
    if not room or room["status"] != "active" or room.get("computer_thinking"):
        return

    board         = make_board(room["moves"])
    gpt_is_white  = room["white"] and room["white"].get("is_chatgpt")
    gpt_is_black  = room["black"] and room["black"].get("is_chatgpt")
    if board.turn == chess.WHITE and not gpt_is_white:
        return
    if board.turn == chess.BLACK and not gpt_is_black:
        return

    room["computer_thinking"] = True
    try:
        result_dict = await chatgpt_move(room["moves"])
        if not result_dict:
            return

        uci  = result_dict["move"]
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            print(f"[chatgpt] mossa illegale dopo retry: {uci}")
            return

        board.push(move)
        room["moves"].append(uci)
        room["move_timestamps"].append(datetime.now())
        room["last_engine_stats"] = {
            "chatgpt_reasoning": result_dict.get("reasoning", ""),
        }
        room["fen"] = board.fen()

        res = check_result(board)
        if res:
            result_str, reason = res
            room["status"] = "finished"
            room["result"] = result_str
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
        else:
            turn = "w" if board.turn == chess.WHITE else "b"
            await broadcast(room_id, {
                "type":         "move",
                "uci":          uci,
                "fen":          room["fen"],
                "turn":         turn,
                "moves":        room["moves"],
                "move_ts":      datetime.now().timestamp(),
                "engine_stats": room["last_engine_stats"],
            })
            # Loop automatico per partite AI vs AI
            if room["type"] in ("CvChatGPT", "CvC"):
                asyncio.create_task(trigger_auto_move(room_id))
    finally:
        room["computer_thinking"] = False


async def trigger_auto_move(room_id: str):
    """
    Per CvChatGPT: determina quale AI deve muovere in base al turno
    e schedula il task appropriato.
    """
    room = rooms.get(room_id)
    if not room or room["status"] != "active":
        return
    # Piccola pausa per non saturare la CPU e rendere la partita guardabile
    await asyncio.sleep(0.5)
    board = make_board(room["moves"])
    slot  = room["white"] if board.turn == chess.WHITE else room["black"]
    if not slot:
        return
    if slot.get("is_chatgpt"):
        asyncio.create_task(trigger_chatgpt_move(room_id))
    elif slot.get("is_computer"):
        asyncio.create_task(trigger_computer_move(room_id))

# ── HTTP ─────────────────────────────────────────────────────────────────────
@app.get("/")
async def home():
    return HTMLResponse((STATIC_DIR / "index.html").read_text())

@app.get("/room/{room_id}")
async def room_page(room_id: str):
    if room_id not in rooms:
        raise HTTPException(404, "Stanza non trovata")
    return HTMLResponse((STATIC_DIR / "room.html").read_text())

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
async def create_room(body: CreateRoomBody):
    color = body.color
    if color == "random":
        color = random.choice(["white", "black"])

    nick     = body.nickname.strip() or "Anonimo"
    room_id  = uuid.uuid4().hex[:6].upper()
    think_ms = max(500, (body.think_sec or 3) * 1000)

    if body.type == "UvsU":
        white_slot = {"nick": nick, "is_computer": False, "is_chatgpt": False} if color == "white" else None
        black_slot = {"nick": nick, "is_computer": False, "is_chatgpt": False} if color == "black" else None
        status     = "waiting"
        return_color = color

    elif body.type == "UvsC":
        comp_nick = f"Engine (d{_think_ms_to_depth(think_ms)})"
        if color == "white":
            white_slot = {"nick": nick,      "is_computer": False, "is_chatgpt": False}
            black_slot = {"nick": comp_nick, "is_computer": True,  "is_chatgpt": False}
        else:
            white_slot = {"nick": comp_nick, "is_computer": True,  "is_chatgpt": False}
            black_slot = {"nick": nick,      "is_computer": False, "is_chatgpt": False}
        status       = "active"
        return_color = color

    elif body.type == "UvChatGPT":
        gpt_nick = "ChatGPT 🤖"
        if color == "white":
            white_slot = {"nick": nick,     "is_computer": False, "is_chatgpt": False}
            black_slot = {"nick": gpt_nick, "is_computer": True,  "is_chatgpt": True}
        else:
            white_slot = {"nick": gpt_nick, "is_computer": True,  "is_chatgpt": True}
            black_slot = {"nick": nick,     "is_computer": False, "is_chatgpt": False}
        status       = "active"
        return_color = color

    elif body.type == "CvChatGPT":
        # "color" indica il colore del motore; ChatGPT gioca l'altro lato
        # Il creatore diventa spettatore
        eng_nick = f"Engine (d{_think_ms_to_depth(think_ms)})"
        gpt_nick = "ChatGPT 🤖"
        if color == "white":
            white_slot = {"nick": eng_nick, "is_computer": True, "is_chatgpt": False}
            black_slot = {"nick": gpt_nick, "is_computer": True, "is_chatgpt": True}
        else:
            white_slot = {"nick": gpt_nick, "is_computer": True, "is_chatgpt": True}
            black_slot = {"nick": eng_nick, "is_computer": True, "is_chatgpt": False}
        status       = "active"
        return_color = "spectator"

    elif body.type == "CvC":
        # Motore vs Motore — entrambi i lati sono engine, il creatore spetta
        d = _think_ms_to_depth(think_ms)
        white_slot = {"nick": f"Engine-W (d{d})", "is_computer": True, "is_chatgpt": False}
        black_slot = {"nick": f"Engine-B (d{d})", "is_computer": True, "is_chatgpt": False}
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

    # Partite che partono subito: imposta game_started_at
    if status == "active":
        rooms[room_id]["game_started_at"] = datetime.now()

    return JSONResponse({"room_id": room_id, "color": return_color})

class JoinBody(BaseModel):
    nickname: str

@app.post("/api/rooms/{room_id}/join")
async def join_room(room_id: str, body: JoinBody):
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(404, "Stanza non trovata")
    if room["type"] != "UvsU" or room["status"] != "waiting":
        raise HTTPException(400, "Stanza non disponibile")

    nick = body.nickname.strip() or "Anonimo"
    if room["white"] is None:
        room["white"] = {"nick": nick, "is_computer": False, "is_chatgpt": False}
        color = "white"
    elif room["black"] is None:
        room["black"] = {"nick": nick, "is_computer": False, "is_chatgpt": False}
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

    # Manda lo stato corrente al client appena connesso
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

    # Trigger mossa AI iniziale se necessario
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

    # Nelle partite interamente automatiche non si accettano mosse umane
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

    res = check_result(board)
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
        # Trigger risposta AI
        if room["type"] == "UvsC":
            asyncio.create_task(trigger_computer_move(room_id))
        elif room["type"] == "UvChatGPT":
            asyncio.create_task(trigger_chatgpt_move(room_id))

async def handle_resign(room_id: str, color: str):
    room = rooms.get(room_id)
    if not room or room["status"] != "active":
        return
    result = "0-1" if color == "white" else "1-0"
    room["status"] = "finished"
    room["result"] = result
    await broadcast(room_id, {
        "type":   "game_over",
        "result": result,
        "reason": ("Il bianco" if color == "white" else "Il nero") + " ha abbandonato",
        "fen":    room["fen"],
        "moves":  room["moves"],
    })

# ── Avvio ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
