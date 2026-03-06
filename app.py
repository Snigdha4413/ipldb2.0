from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from functools import wraps
import os
import time
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "ipl-auction-secret-key-2024")

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

# In-memory auction state (resets on redeploy — fine for demo)
auction_state = {
    "active_player_id": None,
    "timer_end": None,       # epoch time when timer ends
    "paused": False,
    "time_remaining": 60,    # seconds left when paused
}

# --- AUTH HELPERS ---
def get_current_user():
    return session.get("user")

def is_admin():
    return session.get("role") == "admin"

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# --- ONE-TIME SETUP ROUTE (delete after use!) ---
@app.route("/setup-users")
def setup_users():
    users = [
        {"username": "admin",   "password": "admin123", "role": "admin"},
        {"username": "client1", "password": "pass123",  "role": "client"},
        {"username": "client2", "password": "pass123",  "role": "client"},
    ]
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM users"))
        for u in users:
            conn.execute(
                text("INSERT INTO users (username, password, role) VALUES (:u, :p, :r)"),
                {"u": u["username"], "p": generate_password_hash(u["password"]), "r": u["role"]}
            )
        conn.commit()
    return "✅ Users seeded successfully! Now remove this route from app.py."


# --- LOGIN ---
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        with engine.connect() as conn:
            user = conn.execute(
                text("SELECT * FROM users WHERE username = :u"), {"u": username}
            ).fetchone()
        if user and check_password_hash(user.password, password):
            session["user"] = user.username
            session["role"] = user.role
            return redirect(url_for("index"))
        else:
            error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- HOME: Show Teams ---
@app.route("/")
@login_required
def index():
    with engine.connect() as conn:
        teams = conn.execute(
            text("SELECT DISTINCT team FROM players ORDER BY team")
        ).fetchall()
    return render_template("index.html", teams=teams,
                           user=get_current_user(), is_admin=is_admin())


# --- TEAM PAGE ---
@app.route("/team/<team_name>")
@login_required
def team(team_name):
    with engine.connect() as conn:
        players = conn.execute(
            text("SELECT * FROM players WHERE team = :team"), {"team": team_name}
        ).fetchall()

    player_data = []
    with engine.connect() as conn:
        for p in players:
            highest = conn.execute(
                text("SELECT MAX(bid_amount) FROM bids WHERE player_id = :id"), {"id": p.id}
            ).scalar() or 0
            top_bidder = conn.execute(
                text("SELECT bidder FROM bids WHERE player_id=:id ORDER BY bid_amount DESC LIMIT 1"),
                {"id": p.id}
            ).scalar()
            player_data.append({
                "id": p.id, "name": p.name, "country": p.country,
                "role": p.role, "team": p.team,
                "ipl_runs": getattr(p, 'ipl_runs', 0) or 0,
                "ipl_wickets": getattr(p, 'ipl_wickets', 0) or 0,
                "ipl_matches": getattr(p, 'ipl_matches', 0) or 0,
                "strike_rate": float(getattr(p, 'strike_rate', 0) or 0),
                "economy": float(getattr(p, 'economy', 0) or 0),
                "base_price": getattr(p, 'base_price', 0) or 0,
                "photo_url": getattr(p, 'photo_url', '') or '',
                "highest_bid": highest,
                "top_bidder": top_bidder or "No bids yet"
            })

    return render_template("team.html", players=player_data, team_name=team_name,
                           user=get_current_user(), is_admin=is_admin())


# --- PLAYER PAGE ---
@app.route("/player/<int:id>")
@login_required
def player(id):
    with engine.connect() as conn:
        p = conn.execute(text("SELECT * FROM players WHERE id = :id"), {"id": id}).fetchone()
        highest = conn.execute(
            text("SELECT MAX(bid_amount) FROM bids WHERE player_id = :id"), {"id": id}
        ).scalar() or 0
        top_bidder = conn.execute(
            text("SELECT bidder FROM bids WHERE player_id=:id ORDER BY bid_amount DESC LIMIT 1"),
            {"id": id}
        ).scalar()
        bid_history = conn.execute(
            text("SELECT bidder, bid_amount, bid_time FROM bids WHERE player_id=:id ORDER BY bid_amount DESC"),
            {"id": id}
        ).fetchall()

    return render_template("player.html", player=p, highest=highest,
                           top_bidder=top_bidder, bid_history=bid_history,
                           user=get_current_user(), is_admin=is_admin())


# --- AUCTION ROOM ---
@app.route("/auction")
@login_required
def auction_room():
    active_id = auction_state["active_player_id"]
    active_player = None
    highest = 0
    top_bidder = None
    bid_history = []

    if active_id:
        with engine.connect() as conn:
            active_player = conn.execute(
                text("SELECT * FROM players WHERE id = :id"), {"id": active_id}
            ).fetchone()
            highest = conn.execute(
                text("SELECT MAX(bid_amount) FROM bids WHERE player_id = :id"), {"id": active_id}
            ).scalar() or 0
            top_bidder = conn.execute(
                text("SELECT bidder FROM bids WHERE player_id=:id ORDER BY bid_amount DESC LIMIT 1"),
                {"id": active_id}
            ).scalar()
            bid_history = conn.execute(
                text("SELECT bidder, bid_amount, bid_time FROM bids WHERE player_id=:id ORDER BY bid_amount DESC LIMIT 10"),
                {"id": active_id}
            ).fetchall()

    # Compute time remaining
    time_remaining = 0
    if auction_state["paused"]:
        time_remaining = auction_state["time_remaining"]
    elif auction_state["timer_end"]:
        time_remaining = max(0, int(auction_state["timer_end"] - time.time()))

    with engine.connect() as conn:
        all_players = conn.execute(text("SELECT id, name, team FROM players ORDER BY team, name")).fetchall()

    return render_template("auction.html",
                           active_player=active_player,
                           highest=highest,
                           top_bidder=top_bidder,
                           bid_history=bid_history,
                           all_players=all_players,
                           time_remaining=time_remaining,
                           paused=auction_state["paused"],
                           user=get_current_user(),
                           is_admin=is_admin())


# --- ADMIN: Set active auction player ---
@app.route("/auction/set-player", methods=["POST"])
@login_required
def set_auction_player():
    if not is_admin():
        return jsonify({"error": "Admins only"}), 403
    player_id = request.form.get("player_id")
    auction_state["active_player_id"] = int(player_id) if player_id else None
    auction_state["timer_end"] = time.time() + 60
    auction_state["paused"] = False
    auction_state["time_remaining"] = 60
    return redirect(url_for("auction_room"))


# --- ADMIN: Pause/Resume timer ---
@app.route("/auction/pause", methods=["POST"])
@login_required
def pause_auction():
    if not is_admin():
        return jsonify({"error": "Admins only"}), 403
    if not auction_state["paused"]:
        # Pause: save remaining time
        remaining = max(0, int(auction_state["timer_end"] - time.time()))
        auction_state["time_remaining"] = remaining
        auction_state["paused"] = True
    else:
        # Resume: set new end time from remaining
        auction_state["timer_end"] = time.time() + auction_state["time_remaining"]
        auction_state["paused"] = False
    return redirect(url_for("auction_room"))


# --- ADMIN: Reset timer ---
@app.route("/auction/reset-timer", methods=["POST"])
@login_required
def reset_timer():
    if not is_admin():
        return jsonify({"error": "Admins only"}), 403
    auction_state["timer_end"] = time.time() + 60
    auction_state["paused"] = False
    auction_state["time_remaining"] = 60
    return redirect(url_for("auction_room"))


# --- API: Get live auction state (for polling) ---
@app.route("/auction/state")
@login_required
def auction_state_api():
    active_id = auction_state["active_player_id"]
    highest = 0
    top_bidder = None
    recent_bids = []

    if active_id:
        with engine.connect() as conn:
            highest = conn.execute(
                text("SELECT MAX(bid_amount) FROM bids WHERE player_id = :id"), {"id": active_id}
            ).scalar() or 0
            top_bidder = conn.execute(
                text("SELECT bidder FROM bids WHERE player_id=:id ORDER BY bid_amount DESC LIMIT 1"),
                {"id": active_id}
            ).scalar()
            rows = conn.execute(
                text("SELECT bidder, bid_amount FROM bids WHERE player_id=:id ORDER BY bid_amount DESC LIMIT 5"),
                {"id": active_id}
            ).fetchall()
            recent_bids = [{"bidder": r.bidder, "amount": r.bid_amount} for r in rows]

    if auction_state["paused"]:
        time_remaining = auction_state["time_remaining"]
    elif auction_state["timer_end"]:
        time_remaining = max(0, int(auction_state["timer_end"] - time.time()))
    else:
        time_remaining = 0

    return jsonify({
        "active_player_id": active_id,
        "highest": highest,
        "top_bidder": top_bidder,
        "time_remaining": time_remaining,
        "paused": auction_state["paused"],
        "recent_bids": recent_bids
    })


# --- BID ---
@app.route("/bid", methods=["POST"])
@login_required
def bid():
    if is_admin():
        return "Admins cannot place bids!", 403

    player_id = request.form["player_id"]
    bidder = session["user"]
    amount = int(request.form["bid_amount"])

    # Check timer
    if auction_state["paused"]:
        return "Auction is paused!", 400
    if auction_state["timer_end"] and time.time() > auction_state["timer_end"]:
        return "Auction time has ended!", 400

    with engine.connect() as conn:
        current = conn.execute(
            text("SELECT MAX(bid_amount) FROM bids WHERE player_id = :id"), {"id": player_id}
        ).scalar() or 0

        if amount <= current:
            return f"Bid must be higher than ₹{current}!", 400

        conn.execute(
            text("INSERT INTO bids (player_id, bidder, bid_amount) VALUES (:player_id, :bidder, :amount)"),
            {"player_id": player_id, "bidder": bidder, "amount": amount}
        )
        conn.commit()

    # If bid from auction room, redirect back there
    if request.form.get("from_auction"):
        return redirect(url_for("auction_room"))
    return redirect(url_for("player", id=player_id))


if __name__ == "__main__":
    app.run(debug=True)
