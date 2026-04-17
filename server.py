#!/usr/bin/env python3
"""
chess-web — server FastAPI per scacchi multiplayer via browser.
Dipendenze: fastapi, uvicorn[standard], chess
"""

import asyncio
import json
import random
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import chess
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Configurazione ───────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
STATIC_DIR  = BASE_DIR / "static"
ENGINE_PATH = BASE_DIR.parent / "chess-engine" / "build" / "search_cli"

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

async def engine_move(moves: list, think_ms: int) -> Optional[dict]:
    """
    Chiede a search_cli la mossa migliore.
    search_cli accetta: --fen <fen|startpos> --time <ms> --qsearch 1
    e stampa su stdout una riga 'bestmove <uci>'.
    Restituisce un dict con move, depth, score_cp, nodes, nps, time_ms oppure None.
    """
    proc = None
    try:
        # Ricostruiamo la FEN dalla lista mosse con python-chess
        board = make_board(moves)
        fen   = board.fen()

        proc = await asyncio.create_subprocess_exec(
            str(ENGINE_PATH),
            "--fen",     fen,
            "--time",    str(think_ms),
            "--qsearch", "1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        timeout = think_ms / 1000.0 + 10.0
        best_move = None
        depth = None
        score_cp = None
        nodes = None
        nps = None
        time_ms = None

        try:
            while True:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if not raw:
                    break
                line = raw.decode().strip()
                # info depth X score cp Y nodes Z nps W
                if line.startswith("info depth"):
                    parts = line.split()
                    try:
                        idx_depth = parts.index("depth")
                        depth = int(parts[idx_depth + 1])
                    except (ValueError, IndexError):
                        pass
                    try:
                        idx_score = parts.index("cp")
                        score_cp = int(parts[idx_score + 1])
                    except (ValueError, IndexError):
                        pass
                    try:
                        idx_nodes = parts.index("nodes")
                        nodes = int(parts[idx_nodes + 1])
                    except (ValueError, IndexError):
                        pass
                    try:
                        idx_nps = parts.index("nps")
                        nps = float(parts[idx_nps + 1])
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("bestmove"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] != "(none)":
                        best_move = parts[1]
                    break
                elif "time (ms)" in line:
                    # riga: "time (ms)    : X.XX"
                    try:
                        val = line.split(":")[1].strip()
                        time_ms = float(val) * 1000  # converti secondi in ms se necessario
                        # se il valore è già piccolo (< 1000) è probabilmente in secondi
                        # ma potrebbe essere in ms — gestiamo entrambi i casi
                        # Assumiamo che se il valore è già grande è in ms
                        if time_ms < 100:
                            time_ms = float(val) * 1000
                        else:
                            time_ms = float(val)
                    except (IndexError, ValueError):
                        pass
        except asyncio.TimeoutError:
            print("[engine] timeout")

        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()

        if best_move is None:
            return None

        return {
            "move":     best_move,
            "depth":    depth,
            "score_cp": score_cp,
            "nodes":    nodes,
            "nps":      nps,
            "time_ms":  time_ms,
        }

    except Exception as e:
        print(f"[engine] errore: {e}")
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        return None

async def trigger_computer_move(room_id: str):
    """Calcola e applica la mossa del computer per la stanza."""
    room = rooms.get(room_id)
    if not room or room["status"] != "active" or room.get("computer_thinking"):
        return

    board         = make_board(room["moves"])
    comp_is_white = room["white"] and room["white"]["is_computer"]
    comp_color    = chess.WHITE if comp_is_white else chess.BLACK
    if board.turn != comp_color:
        return  # non è il turno del computer

    room["computer_thinking"] = True
    try:
        result_dict = await engine_move(room["moves"], room["think_ms"])
        if not result_dict:
            return

        uci = result_dict["move"]
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
    finally:
        room["computer_thinking"] = False

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
        white_slot = {"nick": nick, "is_computer": False} if color == "white" else None
        black_slot = {"nick": nick, "is_computer": False} if color == "black" else None
        status     = "waiting"
    else:  # UvsC
        comp_nick = f"Computer ({body.think_sec}s)"
        if color == "white":
            white_slot = {"nick": nick,      "is_computer": False}
            black_slot = {"nick": comp_nick, "is_computer": True}
        else:
            white_slot = {"nick": comp_nick, "is_computer": True}
            black_slot = {"nick": nick,      "is_computer": False}
        status = "active"

    board = chess.Board()
    rooms[room_id] = {
        "id":               room_id,
        "type":             body.type,
        "status":           status,
        "created_at":       datetime.now(),
        "white":            white_slot,
        "black":            black_slot,
        "fen":              board.fen(),
        "moves":            [],
        "think_ms":         think_ms,
        "result":           None,
        "computer_thinking": False,
        "game_started_at":  None,
        "move_timestamps":  [],
        "last_engine_stats": None,
    }
    ws_pool[room_id] = []

    # UvsC: la partita è già active, imposta game_started_at
    if body.type == "UvsC":
        rooms[room_id]["game_started_at"] = datetime.now()

    return JSONResponse({"room_id": room_id, "color": color})

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
        room["white"] = {"nick": nick, "is_computer": False}
        color = "white"
    elif room["black"] is None:
        room["black"] = {"nick": nick, "is_computer": False}
        color = "black"
    else:
        raise HTTPException(400, "Stanza piena")

    room["status"] = "active"
    room["game_started_at"] = datetime.now()

    await broadcast(room_id, {
        "type":           "room_full",
        "white":          room["white"]["nick"],
        "black":          room["black"]["nick"],
        "fen":            room["fen"],
        "turn":           "w",
        "moves":          [],
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

    # UvsC: se il computer gioca come bianco, parte subito
    if (room["type"] == "UvsC" and room["status"] == "active"
            and not room["moves"]
            and room["white"] and room["white"]["is_computer"]):
        asyncio.create_task(trigger_computer_move(room_id))

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
        if room["type"] == "UvsC":
            asyncio.create_task(trigger_computer_move(room_id))

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
