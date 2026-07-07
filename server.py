import json, os, socket, threading, time, hashlib, base64, struct
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DATA = "data/chat.json"
USERS_FILE = "data/users.json"
os.makedirs("data", exist_ok=True)

users = {}
db = {"channels": ["general", "testing"], "messages": []}
lock = threading.Lock()
online = {}
online_lock = threading.Lock()

def _load_users():
    global users
    try:
        if os.path.exists(USERS_FILE):
            users = json.loads(open(USERS_FILE).read())
    except: pass
    if not users:
        users["laiton"] = {"pass": hashlib.sha256(b"1103").hexdigest(), "role": "admin"}
        for n in ["nicolas","antoine","theodort","thibault","marlone","tiago","sacha","marius","mathys"]:
            users[n] = {"pass": hashlib.sha256(f"{n}2024".encode()).hexdigest(), "role": "normal"}
        _save_users()

def _save_users():
    open(USERS_FILE, "w").write(json.dumps(users, indent=2))

def _load_db():
    global db
    try:
        if os.path.exists(DATA):
            db = json.loads(open(DATA).read())
    except: pass

def _save_db():
    open(DATA, "w").write(json.dumps(db, indent=2))

_load_users()
_load_db()

def auth(nick, password):
    h = hashlib.sha256(password.encode()).hexdigest()
    u = users.get(nick)
    if u and u["pass"] == h: return u["role"]
    return None

def add_msg(user, text, chan):
    with lock:
        m = {"id": int(time.time()*1000), "user": user, "text": text, "chan": chan}
        db["messages"].append(m)
        if chan not in db["channels"]: db["channels"].append(chan)
        _save_db()
    return m

def msgs_since(after=0):
    return [m for m in db["messages"] if m["id"] > after]

AVATAR_SIZE = 64 * 1024

AVATAR_CACHE = {}
def get_avatar(nick):
    if nick in AVATAR_CACHE: return AVATAR_CACHE[nick]
    u = users.get(nick)
    if u and u.get("avatar"):
        d = u["avatar"]
        AVATAR_CACHE[nick] = d
        return d
    return None

WS_CLIENTS = {}
WS_LOCK = threading.Lock()

def ws_broadcast(data):
    msg = json.dumps(data).encode()
    with WS_LOCK:
        for nick, ws in list(WS_CLIENTS.items()):
            try:
                ws_send(ws, msg)
            except:
                try: ws.close()
                except: pass
                del WS_CLIENTS[nick]

def ws_send(ws, data):
    if isinstance(data, str): data = data.encode()
    length = len(data)
    if length < 126:
        header = bytes([0x81, length])
    elif length < 65536:
        header = bytes([0x81, 126]) + struct.pack(">H", length)
    else:
        header = bytes([0x81, 127]) + struct.pack(">Q", length)
    ws.sendall(header + data)

def ws_handshake(conn, key):
    import hashlib as h
    accept = base64.b64encode(
        h.sha1((key + "258EAFA5-E914-47DA-95CA-5AB5E3F4B6ED").encode()).digest()
    ).decode()
    conn.sendall(
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode()

def ws_read(conn):
    data = conn.recv(4096)
    if not data or len(data) < 2: return None
    b1, b2 = data[0], data[1]
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack(">H", data[2:4])[0]
        offset = 4
    elif length == 127:
        length = struct.unpack(">Q", data[2:10])[0]
        offset = 10
    if masked:
        mask = data[offset:offset+4]
        offset += 4
    payload = data[offset:offset+length]
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if opcode == 8: return None
    if opcode == 1: return payload.decode()
    return None

CRLF = b"\r\n"
def recv_http(conn):
    data = b""
    while True:
        chunk = conn.recv(8192)
        if not chunk: return None
        data += chunk
        if CRLF + CRLF in data: break
    head, _, body = data.partition(CRLF + CRLF)
    first = head.split(CRLF, 1)[0].decode(errors="replace")
    parts = first.split(" ")
    if len(parts) < 2: return None
    method, path = parts[0], parts[1]
    headers = {}
    for line in head.decode(errors="replace").split(CRLF.decode())[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            headers[k.lower()] = v
    cl = int(headers.get("content-length", 0))
    while len(body) < cl:
        chunk = conn.recv(8192)
        if not chunk: break
        body += chunk
    return {"method": method, "path": path, "headers": headers, "body": body}

def json_resp(conn, data, status=200):
    r = json.dumps(data).encode()
    conn.sendall(
        f"HTTP/1.1 {status} OK\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(r)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n\r\n".encode() + r
    )

def html_resp(conn, html, status=200):
    h = html.encode()
    conn.sendall(
        f"HTTP/1.1 {status} OK\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(h)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n\r\n".encode() + h
    )

def img_resp(conn, data, status=200):
    conn.sendall(
        f"HTTP/1.1 {status} OK\r\n"
        f"Content-Type: image/png\r\n"
        f"Content-Length: {len(data)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"Cache-Control: max-age=3600\r\n\r\n".encode() + data
    )

def err_resp(conn, code=400):
    conn.sendall(f"HTTP/1.1 {code} Bad Request\r\nContent-Length: 0\r\n\r\n".encode())

CORS = ("Access-Control-Allow-Origin: *\r\n"
        "Access-Control-Allow-Methods: GET,POST,OPTIONS\r\n"
        "Access-Control-Allow-Headers: Content-Type\r\n")

def handle(conn, req):
    try:
        method, path = req["method"], req["path"]
        parsed = urlparse(path)
        p, qs = parsed.path, parse_qs(parsed.query)
        body = req["body"]

        if method == "OPTIONS":
            conn.sendall(f"HTTP/1.1 204\r\n{CORS}Content-Length: 0\r\n\r\n".encode())

        elif p == "/" and method == "GET":
            html_resp(conn, HTML)

        elif p == "/api/login" and method == "POST":
            j = json.loads(body.decode())
            nick = j.get("user","").lower()
            role = auth(nick, j.get("pass",""))
            if role:
                with online_lock:
                    online[nick] = time.time()
                avatar_b64 = j.get("avatar", "")
                if avatar_b64 and len(avatar_b64) < AVATAR_SIZE:
                    u = users.get(nick)
                    if u:
                        u["avatar"] = avatar_b64
                        AVATAR_CACHE.pop(nick, None)
                        _save_users()
                json_resp(conn, {"ok": True, "role": role})
            else:
                json_resp(conn, {"ok": False}, 401)

        elif p == "/api/avatar" and method == "GET":
            nick = qs.get("user", [""])[0]
            d = get_avatar(nick)
            if d:
                try:
                    img_resp(conn, base64.b64decode(d))
                except:
                    err_resp(conn, 500)
            else:
                img_resp(conn, _gray_circle())

        elif p == "/api/avatar" and method == "POST":
            j = json.loads(body.decode())
            nick = j.get("user","").lower()
            role = auth(nick, j.get("pass",""))
            if not role:
                json_resp(conn, {"ok": False}, 401)
            else:
                avatar_b64 = j.get("avatar", "")
                if avatar_b64 and len(avatar_b64) < AVATAR_SIZE:
                    u = users.get(nick)
                    if u:
                        u["avatar"] = avatar_b64
                        AVATAR_CACHE.pop(nick, None)
                        _save_users()
                    json_resp(conn, {"ok": True})
                else:
                    json_resp(conn, {"ok": False, "error": "Image trop volumineuse"}, 400)

        elif p == "/api/msgs" and method == "GET":
            after = int(qs.get("after", [0])[0])
            json_resp(conn, msgs_since(after))

        elif p == "/api/send" and method == "POST":
            j = json.loads(body.decode())
            user, passwd, text, chan = j.get("user",""), j.get("pass",""), j.get("text",""), j.get("chan","general")
            role = auth(user, passwd)
            if not role:
                json_resp(conn, {"ok": False}, 401)
            else:
                m = add_msg(user, text, chan)
                with online_lock:
                    online[user] = time.time()
                json_resp(conn, m)
                ws_broadcast({"type": "msg", "msg": m})

        elif p == "/api/channels" and method == "GET":
            with lock:
                json_resp(conn, db["channels"])

        elif p == "/api/channels/create" and method == "POST":
            j = json.loads(body.decode())
            user, passwd, chan = j.get("user",""), j.get("pass",""), j.get("chan","")
            role = auth(user, passwd)
            if not role: json_resp(conn, {"ok": False}, 401)
            elif role != "admin": json_resp(conn, {"ok": False}, 403)
            elif not chan: json_resp(conn, {"ok": False}, 400)
            else:
                with lock:
                    if chan not in db["channels"]: db["channels"].append(chan); _save_db()
                json_resp(conn, {"ok": True, "chan": chan})

        elif p == "/api/channels/delete" and method == "POST":
            j = json.loads(body.decode())
            user, passwd, chan = j.get("user",""), j.get("pass",""), j.get("chan","")
            role = auth(user, passwd)
            if not role: json_resp(conn, {"ok": False}, 401)
            elif role != "admin": json_resp(conn, {"ok": False}, 403)
            elif chan == "general": json_resp(conn, {"ok": False}, 400)
            else:
                with lock:
                    if chan in db["channels"]: db["channels"].remove(chan)
                    db["messages"] = [m for m in db["messages"] if m["chan"] != chan]
                    _save_db()
                json_resp(conn, {"ok": True})

        elif p == "/api/online" and method == "GET":
            now = time.time()
            with online_lock:
                ol = [n for n,t in online.items() if now - t < 30]
            json_resp(conn, ol)

        elif p == "/api/users" and method == "GET":
            json_resp(conn, list(users.keys()))

        elif p == "/health":
            conn.sendall(b"HTTP/1.1 200\r\nContent-Length: 2\r\n\r\nok")

        else:
            err_resp(conn, 404)
    except Exception as e:
        try: err_resp(conn, 400)
        except: pass
    finally:
        try: conn.close()
        except: pass

GRAY_CIRCLE = None
def _gray_circle():
    global GRAY_CIRCLE
    if GRAY_CIRCLE: return GRAY_CIRCLE
    import io, struct as st
    w = h = 64
    raw = bytearray()
    for y in range(h):
        for x in range(w):
            cx, cy = w//2, h//2
            r = min(cx, cy) - 1
            d = ((x-cx)**2 + (y-cy)**2) ** 0.5
            if d <= r:
                raw.extend([180, 180, 180, 255])
            else:
                raw.extend([0, 0, 0, 0])
    def png():
        import zlib
        sig = b'\x89PNG\r\n\x1a\n'
        def chunk(t, d):
            c = st.pack('>I', len(d)) + t + d
            c += st.pack('>I', zlib.crc32(c) & 0xffffffff)
            return c
        ihdr = st.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
        raw_data = b''
        for y in range(h):
            raw_data += b'\x00' + bytes(raw[y*w*4:(y+1)*w*4])
        compressed = zlib.compress(raw_data)
        return sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', compressed) + chunk(b'IEND', b'')
    GRAY_CIRCLE = png()
    return GRAY_CIRCLE

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PeerSync v2</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;background:#0f0f1a;color:#e0e0e0;height:100vh;display:flex;flex-direction:column}
#login{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;gap:10px;background:#0f0f1a}
#login h1{color:#00d4aa;font-size:2em;margin-bottom:4px}
#login input,#login .file-label{width:300px;padding:10px 14px;border-radius:8px;border:1px solid #333;background:#1a1a2e;color:#fff;font-size:0.95em;outline:none}
#login input:focus{border-color:#00d4aa}
#login .file-label{display:flex;align-items:center;gap:8px;cursor:pointer;color:#888}
#login .file-label:hover{border-color:#00d4aa;color:#fff}
#login .file-label input{display:none}
#login .avatar-preview{width:48px;height:48px;border-radius:50%;object-fit:cover;border:2px solid #333}
#login .avatar-row{display:flex;align-items:center;gap:10px;width:300px}
#login button{padding:10px 28px;border-radius:8px;border:none;background:#00d4aa;color:#000;font-weight:700;font-size:0.95em;cursor:pointer}
#login button:hover{background:#00e6b3}
#login .error{color:#ff4444;font-size:0.85em;display:none}
#app{display:none;flex:1;flex-direction:column}
#header{background:#1a1a2e;padding:8px 16px;display:flex;align-items:center;gap:10px;border-bottom:1px solid #333}
#header h2{color:#00d4aa;font-size:1em}
#header .nick{color:#00d4aa;font-weight:600;font-size:0.9em}
#header .online{color:#888;font-size:0.8em;margin-left:auto}
#main{display:flex;flex:1;overflow:hidden}
#sidebar{width:200px;background:#12122a;border-right:1px solid #333;display:flex;flex-direction:column}
#sidebar .chan-list{flex:1;overflow-y:auto}
#sidebar .chan{padding:8px 14px;cursor:pointer;border-left:3px solid transparent;display:flex;align-items:center;gap:6px;font-size:0.9em}
#sidebar .chan:hover{background:#1a1a3e}
#sidebar .chan.active{border-left-color:#00d4aa;background:#1a1a3e;color:#00d4aa}
#sidebar .chan .badge{background:#00d4aa;color:#000;border-radius:10px;padding:1px 6px;font-size:0.7em;margin-left:auto}
#chan-header{display:flex;align-items:center;justify-content:space-between;padding:6px 14px;border-bottom:1px solid #333;color:#888;font-size:0.8em;text-transform:uppercase}
#chat{flex:1;display:flex;flex-direction:column}
#msgs{flex:1;overflow-y:auto;padding:12px 16px}
.msg{display:flex;gap:8px;margin-bottom:10px;animation:fadeIn .15s}
@keyframes fadeIn{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}
.msg .avatar{width:32px;height:32px;border-radius:50%;flex-shrink:0;object-fit:cover;background:#333;margin-top:2px}
.msg .avatar-placeholder{width:32px;height:32px;border-radius:50%;flex-shrink:0;background:#555;display:flex;align-items:center;justify-content:center;font-size:14px;color:#999;margin-top:2px}
.msg .content{flex:1;min-width:0}
.msg .head{font-size:0.78em;color:#666;margin-bottom:1px}
.msg .head .name{color:#ffd700;font-weight:600;cursor:pointer}
.msg .head .name.me{color:#00d4aa}
.msg .head .time{color:#555;margin-left:6px}
.msg .body{color:#e0e0e0;line-height:1.4;font-size:0.92em;word-wrap:break-word}
.msg .body .emote{color:#aaa;font-style:italic}
#input-bar{display:flex;padding:10px 16px;gap:8px;border-top:1px solid #333;background:#1a1a2e}
#input-bar input{flex:1;padding:9px 12px;border-radius:8px;border:1px solid #333;background:#0f0f1a;color:#fff;font-size:0.95em;outline:none}
#input-bar input:focus{border-color:#00d4aa}
#input-bar button{padding:9px 18px;border-radius:8px;border:none;background:#00d4aa;color:#000;font-weight:600;font-size:0.9em;cursor:pointer}
#input-bar button:hover{background:#00e6b3}
</style>
</head>
<body>
<div id="login">
<h1>PeerSync v2</h1>
<input id="nick" placeholder="Pseudo" autofocus>
<input id="pass" type="password" placeholder="Mot de passe">
<input id="server" placeholder="Serveur (host:port)" value="serverirc2-production.up.railway.app:443">
<div class="avatar-row">
<label class="file-label">
  <input id="avatar-input" type="file" accept="image/png,image/jpeg,image/gif" onchange="previewAvatar(event)">
  <span id="avatar-label">Banniere (optionnel)</span>
</label>
<img id="avatar-preview" class="avatar-preview" style="display:none">
</div>
<div class="error" id="login-error">Connexion impossible</div>
<button onclick="login()">CONNEXION</button>
</div>
<div id="app">
<div id="header">
<h2>PeerSync v2</h2>
<span class="nick" id="header-nick"></span>
<span class="online" id="header-online"></span>
</div>
<div id="main">
<div id="sidebar">
<div id="chan-header">
<span>Salons</span>
<span id="chan-actions" style="display:none">
<button onclick="createChan()" title="Creer" style="background:none;border:none;color:#00d4aa;cursor:pointer;font-size:1.1em;padding:0 4px">+</button>
<button onclick="deleteChan()" title="Supprimer" style="background:none;border:none;color:#ff4444;cursor:pointer;font-size:1.1em;padding:0 4px">−</button>
</span>
</div>
<div class="chan-list" id="chan-list"></div>
</div>
<div id="chat">
<div id="msgs"></div>
<div id="input-bar">
<input id="input" placeholder="Message..." onkeydown="if(event.key==='Enter')send()">
<button onclick="send()">Envoyer</button>
</div>
</div>
</div>
</div>
<script>
let nick="",pass="",base="",curChan="general",lastId=0,channels=["general"],ws=null,myAvatar=null;
let allMsgs={};

function previewAvatar(e){
  const f=e.target.files[0];
  if(!f)return;
  document.getElementById("avatar-label").textContent=f.name;
  const r=new FileReader();
  r.onload=function(){document.getElementById("avatar-preview").src=r.result;document.getElementById("avatar-preview").style.display="block"};
  r.readAsDataURL(f)
}

async function api(method,path,body){
  try{
    const opts={method,headers:{}};
    if(body){opts.headers["Content-Type"]="application/json";opts.body=JSON.stringify(body)}
    const r=await fetch(base+path,opts);
    if(!r.ok)return null;
    const ct=r.headers.get("content-type")||"";
    if(ct.includes("json"))return await r.json();
    if(ct.includes("image"))return await r.blob();
    return await r.text();
  }catch(e){return null}
}

async function login(){
  nick=document.getElementById("nick").value.trim().toLowerCase();
  pass=document.getElementById("pass").value.trim();
  const hp=document.getElementById("server").value.trim();
  if(!nick||!pass)return;
  let host,port;
  if(hp.includes(":")){[host,port]=hp.split(":");port=parseInt(port)}
  else{host=hp;port=8080}
  base=(port===443?"https://":"http://")+host+(port!==443?":"+port:"");
  const loginData={user:nick,pass};
  const avatarFile=document.getElementById("avatar-input").files[0];
  if(avatarFile){
    const r=new FileReader();
    await new Promise(d=>{r.onload=d;r.readAsDataURL(avatarFile)});
    myAvatar=r.result;
    loginData.avatar=myAvatar
  }
  const r=await api("POST","/api/login",loginData);
  if(!r||!r.ok){document.getElementById("login-error").style.display="block";return}
  document.getElementById("login").style.display="none";
  document.getElementById("app").style.display="flex";
  document.getElementById("header-nick").textContent=nick;
  if(r.role==="admin")document.getElementById("chan-actions").style.display="inline";
  if("Notification" in window&&Notification.permission==="default")Notification.requestPermission();
  const ch=await api("GET","/api/channels");
  if(ch){channels=ch}
  if(!channels.includes("general"))channels.unshift("general");
  const msgs=await api("GET","/api/msgs?after=0");
  if(msgs){for(const m of msgs){allMsgs[m.id]=m;if(m.id>lastId)lastId=m.id}}
  renderChannels();
  switchChan("general");
  startPoll();
  connectWS()
}

function connectWS(){
  try{
    const url=base.replace("http://","ws://").replace("https://","wss://");
    ws=new WebSocket(url+"/ws?nick="+encodeURIComponent(nick));
    ws.onmessage=function(e){
      try{
        const d=JSON.parse(e.data);
        if(d.type==="online")updateOnline(d.users);
        if(d.type==="msg"){handleNewMsg(d.msg)}
      }catch(e2){}
    };
    ws.onclose=function(){setTimeout(connectWS,3000)}
  }catch(e){}
}

let pollTimer;
function startPoll(){
  pollTimer=setInterval(async()=>{
    const msgs=await api("GET","/api/msgs?after="+lastId);
    if(msgs){for(const m of msgs){handleNewMsg(m);if(m.id>lastId)lastId=m.id}}
    const ch=await api("GET","/api/channels");
    if(ch&&JSON.stringify(ch)!==JSON.stringify(channels)){channels=ch;renderChannels()}
    const ol=await api("GET","/api/online");
    if(ol)updateOnline(ol)
  },2000)
}

function handleNewMsg(m){
  if(allMsgs[m.id])return;
  allMsgs[m.id]=m;
  if(curChan==="general"||m.chan===curChan)renderMsg(m);
  updateUnread(m);
  if(m.user!==nick){
    playSound();
    if(document.hidden&&"Notification" in window&&Notification.permission==="granted"){
      new Notification("PeerSync - "+m.user,{body:m.text.slice(0,80),icon:base+"/api/avatar?user="+encodeURIComponent(m.user)})
    }
  }
}

function updateUnread(m){
  if(m.chan!==curChan){
    const el=document.getElementById("badge-"+m.chan);
    if(el){
      const c=parseInt(el.textContent)||0;
      el.textContent=c+1;el.style.display="inline"
    }
  }
}

function renderChannels(){
  const el=document.getElementById("chan-list");el.innerHTML="";
  for(const c of channels){
    const d=document.createElement("div");d.className="chan"+(c===curChan?" active":"");
    d.innerHTML="#"+c+'<span class="badge" id="badge-'+c+'" style="display:none">0</span>';
    d.onclick=function(){switchChan(c)};el.appendChild(d)
  }
}

function switchChan(c){
  curChan=c;
  document.querySelectorAll(".chan").forEach(e=>e.classList.remove("active"));
  const ch=document.getElementById("chan-list").children;
  for(let i=0;i<ch.length;i++){if(ch[i].textContent.trim().startsWith("#"+c))ch[i].classList.add("active")}
  document.getElementById("msgs").innerHTML="";
  const badge=document.getElementById("badge-"+c);
  if(badge){badge.textContent="0";badge.style.display="none"}
  for(const id in allMsgs){
    const m=allMsgs[id];
    if(m.chan===c)renderMsg(m)
  }
  const el=document.getElementById("msgs");el.scrollTop=el.scrollHeight
}

function renderMsg(m){
  const el=document.getElementById("msgs");
  const d=document.createElement("div");d.className="msg";
  const t=new Date(m.id/1000);
  const ts=t.getHours().toString().padStart(2,"0")+":"+t.getMinutes().toString().padStart(2,"0");
  const av=document.createElement("img");av.className="avatar";
  av.src=base+"/api/avatar?user="+encodeURIComponent(m.user);
  av.onerror=function(){
    const p=document.createElement("div");p.className="avatar-placeholder";
    p.textContent=m.user[0].toUpperCase();
    this.parentNode.replaceChild(p,this)
  };
  d.appendChild(av);
  const c=document.createElement("div");c.className="content";
  const h=document.createElement("div");h.className="head";
  const n=document.createElement("span");n.className="name"+(m.user===nick?" me":"");n.textContent=m.user;
  h.append(n,document.createElement("time"));h.querySelector("time").textContent=ts;h.querySelector("time").style.cssText="color:#555;margin-left:6px;font-size:0.9em";
  c.appendChild(h);
  const b=document.createElement("div");b.className="body";
  if(m.text.startsWith("/me ")){b.innerHTML='<span class="emote">* '+m.user+" "+m.text.slice(4)+"</span>"}
  else{b.textContent=m.text}
  c.appendChild(b);d.appendChild(c);el.appendChild(d);
  el.scrollTop=el.scrollHeight
}

function send(){
  const i=document.getElementById("input");const t=i.value.trim();
  if(!t)return;i.value="";
  api("POST","/api/send",{user:nick,pass,text:t,chan:curChan}).then(m=>{
    if(m&&m.id){handleNewMsg(m);if(m.id>lastId)lastId=m.id}
  })
}

function updateOnline(users){
  const el=document.getElementById("header-online");
  el.textContent=users?.length?"● "+users.length+" en ligne":""
}

async function createChan(){
  const name=prompt("Nom du nouveau salon:");
  if(!name||!name.trim())return;
  const c=name.trim().toLowerCase().replace(/\\s+/g,"-");
  const r=await api("POST","/api/channels/create",{user:nick,pass,chan:c});
  if(r&&r.ok){
    if(!channels.includes(c)){channels.push(c);renderChannels()}
    switchChan(c)
  }else{alert("Impossible de creer le salon")}
}

async function deleteChan(){
  if(curChan==="general"){alert("Impossible de supprimer #general");return}
  if(!confirm("Supprimer #"+curChan+" ?"))return;
  const r=await api("POST","/api/channels/delete",{user:nick,pass,chan:curChan});
  if(r&&r.ok){
    const idx=channels.indexOf(curChan);
    if(idx>-1)channels.splice(idx,1);
    renderChannels();
    switchChan("general")
  }else{alert("Impossible de supprimer")}
}
</script>
</body>
</html>"""

PORT = int(os.environ.get("PORT", 8080))
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", PORT))
srv.listen(128)
print(f"PeerSync v2 ready on http://0.0.0.0:{PORT}", flush=True)

while True:
    try:
        c, a = srv.accept()
        req = recv_http(c)
        if req is None:
            try: c.close()
            except: pass
            continue
        hdrs = req["headers"]
        if hdrs.get("upgrade","").lower() == "websocket":
            ws_handshake(c, hdrs.get("sec-websocket-key",""))
            nick = parse_qs(urlparse(req["path"]).query).get("nick", [""])[0]
            if nick:
                with WS_LOCK:
                    WS_CLIENTS[nick] = c
            try:
                while True:
                    msg = ws_read(c)
                    if msg is None: break
            except: pass
            finally:
                with WS_LOCK:
                    WS_CLIENTS.pop(nick, None)
                try: c.close()
                except: pass
        else:
            threading.Thread(target=handle, args=(c, req), daemon=True).start()
    except KeyboardInterrupt: break
    except: pass
