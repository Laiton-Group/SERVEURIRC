import json, os, sys, threading, time, urllib.request, urllib.error, hashlib, base64, io
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import tkinter.font as tkfont

_FONT_CACHE = None
def _font(size=10, bold=False, mono=False):
    global _FONT_CACHE
    if _FONT_CACHE is None:
        _FONT_CACHE = set(f.lower() for f in tkfont.families())
    families = (["Consolas", "DejaVu Sans Mono", "Liberation Mono", "Courier New", "Menlo"]
                if mono else
                ["Segoe UI", "DejaVu Sans", "Liberation Sans", "Arial", "Helvetica Neue"])
    for f in families:
        if f.lower() in _FONT_CACHE:
            return (f, size, "bold") if bold else (f, size)
    return ("TkFixedFont" if mono else "TkDefaultFont", size)

BG = "#0f0f1a"
FG = "#e0e0e0"
ACCENT = "#00d4aa"
DARK2 = "#1a1a2e"
DARK3 = "#12122a"
SEL = "#1a4a6e"

class APIClient:
    def __init__(self):
        self.base = ""
        self.nick = ""
        self.password = ""
        self.role = "normal"
        self.channels = ["general"]
        self.all_msgs = {}
        self.last_id = 0
        self.running = True
        self.avatar_b64 = None

    def _req(self, path, data=None):
        try:
            if data:
                req = urllib.request.Request(
                    f"{self.base}{path}",
                    data=json.dumps(data).encode(),
                    headers={"Content-Type": "application/json"}
                )
                resp = urllib.request.urlopen(req, timeout=5)
            else:
                resp = urllib.request.urlopen(f"{self.base}{path}", timeout=5)
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return json.loads(resp.read())
            return resp.read()
        except: return None

    def connect(self, host, port, nick, password, avatar_b64=None):
        self.nick = nick
        self.password = password
        self.base = f"http://{host}:{port}"
        if port == 443: self.base = f"https://{host}"
        data = {"user": nick, "pass": password}
        if avatar_b64:
            data["avatar"] = avatar_b64
            self.avatar_b64 = avatar_b64
        r = self._req("/api/login", data)
        if not r or not r.get("ok"): return False
        self.role = r.get("role", "normal")
        ch = self._req("/api/channels")
        if ch is not None:
            self.channels = ch if isinstance(ch, list) else ["general"]
            if "general" not in self.channels: self.channels.insert(0, "general")
            msgs = self._req("/api/msgs?after=0")
            if msgs is None: return False
            for m in msgs:
                self.all_msgs[m["id"]] = m
                if m["id"] > self.last_id: self.last_id = m["id"]
            return True
        return False

    def send_msg(self, channel, text):
        m = self._req("/api/send", {"user": self.nick, "pass": self.password, "text": text, "chan": channel})
        if m and m.get("id"):
            self.all_msgs[m["id"]] = m
            if m["id"] > self.last_id: self.last_id = m["id"]
            return m
        return None

    def create_channel(self, chan):
        r = self._req("/api/channels/create", {"user": self.nick, "pass": self.password, "chan": chan})
        return r.get("ok") if r else False

    def delete_channel(self, chan):
        r = self._req("/api/channels/delete", {"user": self.nick, "pass": self.password, "chan": chan})
        return r.get("ok") if r else False

    def get_avatar(self, user):
        data = self._req(f"/api/avatar?user={user}")
        if data and isinstance(data, bytes):
            return data
        return None

    def poll(self):
        msgs = self._req(f"/api/msgs?after={self.last_id}")
        if msgs is None: self.running = False; return []
        new = []
        for m in msgs:
            if m["id"] not in self.all_msgs:
                self.all_msgs[m["id"]] = m
                new.append(m)
            if m["id"] > self.last_id: self.last_id = m["id"]
        ch = self._req("/api/channels")
        if ch and isinstance(ch, list):
            for c in ch:
                if c not in self.channels: self.channels.append(c)
        return new


class AvatarCache:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def get(self, user, client):
        with self.lock:
            if user in self.cache:
                return self.cache[user]
        data = client.get_avatar(user)
        if data:
            try:
                photo = tk.PhotoImage(data=data)
                with self.lock:
                    self.cache[user] = photo
                return photo
            except: pass
        return None


class ChatGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("PeerSync v2")
        self.root.geometry("900x600")
        self.root.configure(bg=BG)
        self.client = APIClient()
        self.avatars = AvatarCache()
        self.avatar_imgs = {}
        self._style()
        self._connect_dialog()
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

    def _style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=DARK2, selectbackground=SEL, font=_font(10))
        s.configure("Treeview", background=DARK2, foreground=FG, fieldbackground=DARK2, rowheight=26, font=_font(10))
        s.map("Treeview", background=[("selected", SEL)], foreground=[("selected", ACCENT)])
        s.configure("TEntry", fieldbackground=DARK2, foreground=FG)
        s.configure("TButton", background=DARK3, foreground=FG, font=_font(9))
        s.map("TButton", background=[("active", SEL)])

    def _load_config(self):
        cfg = {}
        try:
            d = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(d, "config.json")) as f:
                cfg = json.load(f)
        except: pass
        return cfg

    def _connect_dialog(self):
        self._style()
        cfg = self._load_config()
        win = tk.Toplevel(self.root, bg=BG)
        win.title("Connexion")
        win.geometry("380x320+200+200")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._quit)
        win.update_idletasks()
        win.grab_set()
        self.avatar_path = None
        self.avatar_preview = None

        tk.Label(win, text="Serveur (host:port):", bg=BG, fg=FG, anchor="w", font=_font(10)).pack(fill="x", padx=15, pady=(15,0))
        self.host_entry = tk.Entry(win, bg=DARK2, fg=FG, insertbackground=ACCENT, font=_font(10), relief="flat", bd=4)
        self.host_entry.insert(0, cfg.get("hub", "serverirc2-production.up.railway.app:443"))
        self.host_entry.pack(fill="x", padx=15, pady=3)

        tk.Label(win, text="Pseudonyme:", bg=BG, fg=FG, anchor="w", font=_font(10)).pack(fill="x", padx=15, pady=(2,0))
        self.nick_entry = tk.Entry(win, bg=DARK2, fg=FG, insertbackground=ACCENT, font=_font(10), relief="flat", bd=4)
        import getpass; self.nick_entry.insert(0, getpass.getuser()[:20])
        self.nick_entry.pack(fill="x", padx=15, pady=3)

        tk.Label(win, text="Mot de passe:", bg=BG, fg=FG, anchor="w", font=_font(10)).pack(fill="x", padx=15, pady=(2,0))
        self.pass_entry = tk.Entry(win, bg=DARK2, fg=FG, insertbackground=ACCENT, font=_font(10), relief="flat", bd=4, show="*")
        self.pass_entry.insert(0, cfg.get("pass", ""))
        self.pass_entry.pack(fill="x", padx=15, pady=3)

        af = tk.Frame(win, bg=BG)
        af.pack(fill="x", padx=15, pady=3)
        tk.Button(af, text="Choisir banniere", command=self._pick_avatar, bg=DARK3, fg=ACCENT, font=_font(9), relief="flat", bd=3, cursor="hand2").pack(side="left")
        self.avatar_label = tk.Label(af, text="Aucune", bg=BG, fg="#666", font=_font(9))
        self.avatar_label.pack(side="left", padx=8)

        tk.Button(win, text="CONNEXION", command=self._do_connect, bg=DARK3, fg=ACCENT, font=_font(10, bold=True), relief="flat", bd=4, padx=20, cursor="hand2").pack(pady=8)

    def _pick_avatar(self):
        path = filedialog.askopenfilename(
            title="Choisir une banniere",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.bmp")]
        )
        if path:
            self.avatar_path = path
            self.avatar_label.config(text=os.path.basename(path)[:20])

    def _img_to_b64(self, path):
        try:
            with open(path, "rb") as f:
                raw = f.read()
            return base64.b64encode(raw).decode()
        except: return None

    def _do_connect(self):
        hp = self.host_entry.get().strip(); nick = self.nick_entry.get().strip()[:20].lower(); pwd = self.pass_entry.get().strip()
        if ":" in hp: host, port = hp.rsplit(":", 1); port = int(port)
        else: host, port = hp, 8080
        if not nick: nick = "user"
        if not pwd: messagebox.showerror("Erreur", "Mot de passe requis"); return
        avatar_b64 = None
        if self.avatar_path:
            avatar_b64 = self._img_to_b64(self.avatar_path)
        if self.client.connect(host, port, nick, pwd, avatar_b64):
            self._build_ui(); self._start_poll()
        else:
            messagebox.showerror("Erreur", "Connexion impossible")

    def _build_ui(self):
        for w in self.root.winfo_children(): w.destroy()
        self.root.configure(bg=BG)
        panes = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, sashwidth=2, bg=DARK3)
        panes.pack(fill="both", expand=True)
        left = tk.Frame(panes, bg=DARK3); right = tk.Frame(panes, bg=BG)
        panes.add(left, width=200, minsize=130); panes.add(right, width=700, minsize=320)

        top = tk.Frame(left, bg=DARK2)
        top.pack(fill="x")
        role_tag = " @" if self.client.role == "admin" else ""
        tk.Label(top, text=f"PeerSync v2{role_tag}", bg=DARK2, fg=ACCENT, font=_font(10, bold=True)).pack(padx=8, pady=4, anchor="w")

        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        self.tree.insert("", "end", "general", text="#general", open=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_channel_select)
        self.tree.selection_set("general")

        btn_f = tk.Frame(left, bg=DARK3)
        btn_f.pack(fill="x", pady=3, padx=4)
        if self.client.role == "admin":
            tk.Button(btn_f, text="+", command=self._join_dialog, bg=DARK2, fg=ACCENT, font=_font(9, bold=True), relief="flat", bd=3, cursor="hand2").pack(side="left", fill="x", expand=True, padx=(0,1))
            tk.Button(btn_f, text="-", command=self._delete_dialog, bg="#4a0000", fg="#ff4444", font=_font(9, bold=True), relief="flat", bd=3, cursor="hand2").pack(side="right", fill="x", expand=True, padx=(1,0))
        else:
            tk.Label(btn_f, text=self.client.nick, bg=DARK3, fg="#888", font=_font(9)).pack()

        hdr = tk.Frame(right, bg=DARK2)
        hdr.pack(fill="x")
        self.chan_label = tk.Label(hdr, text="#general", bg=DARK2, fg=ACCENT, font=_font(10, bold=True), anchor="w")
        self.chan_label.pack(fill="x", padx=10, pady=4)

        self.msg_frame = tk.Frame(right, bg=BG)
        self.msg_frame.pack(fill="both", expand=True)
        self.msg_canvas = tk.Canvas(self.msg_frame, bg=BG, highlightthickness=0)
        self.msg_scroll = tk.Scrollbar(self.msg_frame, orient="vertical", command=self.msg_canvas.yview)
        self.msg_inner = tk.Frame(self.msg_canvas, bg=BG)
        self.msg_inner.bind("<Configure>", lambda e: self.msg_canvas.configure(scrollregion=self.msg_canvas.bbox("all")))
        self.msg_canvas.create_window((0, 0), window=self.msg_inner, anchor="nw", tags="inner")
        self.msg_canvas.configure(yscrollcommand=self.msg_scroll.set)
        self.msg_canvas.pack(side="left", fill="both", expand=True)
        self.msg_scroll.pack(side="right", fill="y")

        bottom = tk.Frame(right, bg=BG)
        bottom.pack(fill="x", padx=6, pady=(0,6))
        self.entry = tk.Entry(bottom, bg=DARK2, fg=FG, insertbackground=ACCENT, font=_font(11), relief="flat", bd=5)
        self.entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.entry.bind("<Return>", self._send_msg)
        tk.Button(bottom, text="ENVOYER", command=self._send_msg, bg=DARK2, fg=ACCENT, font=_font(9, bold=True), relief="flat", bd=3, padx=12, cursor="hand2").pack(side="right", padx=(4,0))

        self.root.title(f"PeerSync v2 - {self.client.nick}")
        self._refresh_msgs()

    def _join_dialog(self):
        ch = simpledialog.askstring("Creer salon", "Nom:", parent=self.root)
        if ch and ch.strip():
            ch = ch.strip().lower().replace(" ", "-")
            if self.client.create_channel(ch):
                if ch not in self.client.channels:
                    self.client.channels.append(ch)
                    self.tree.insert("", "end", ch, text=f"#{ch}", open=True)
                self.tree.selection_set(ch)
            else:
                messagebox.showerror("Erreur", "Impossible de creer le salon")

    def _delete_dialog(self):
        ch = self._current_channel()
        if ch == "general": messagebox.showerror("Erreur", "Impossible de supprimer #general"); return
        if messagebox.askyesno("Supprimer", f"Supprimer #{ch} ?"):
            if self.client.delete_channel(ch):
                self.tree.delete(ch)
                if ch in self.client.channels: self.client.channels.remove(ch)
                self.tree.selection_set("general")
                self._refresh_msgs()
            else:
                messagebox.showerror("Erreur", "Impossible de supprimer")

    def _on_channel_select(self, e):
        sel = self.tree.selection()
        if sel:
            self.chan_label.configure(text=f"#{sel[0]}")
            self._refresh_msgs()

    def _current_channel(self):
        sel = self.tree.selection(); return sel[0] if sel else "general"

    def _send_msg(self, event=None):
        text = self.entry.get().strip()
        if not text: return
        ch = self._current_channel()
        self.client.send_msg(ch, text)
        self.entry.delete(0, "end")
        self._refresh_msgs()

    def _create_msg_widget(self, m):
        nick = m["user"]
        text = m["text"]
        t = time.strftime("%H:%M", time.localtime(m["id"]/1000))
        is_me = nick == self.client.nick

        frame = tk.Frame(self.msg_inner, bg=BG)
        frame.pack(fill="x", padx=10, pady=(0, 4))

        avatar_frame = tk.Frame(frame, width=34, height=34, bg=BG)
        avatar_frame.pack(side="left")
        avatar_frame.pack_propagate(False)

        img = self.avatars.get(nick, self.client)
        if img:
            lbl = tk.Label(avatar_frame, image=img, bg=BG)
            lbl.image = img
            lbl.pack()
        else:
            lbl = tk.Label(avatar_frame, text=nick[0].upper(), bg="#555", fg="#999", font=_font(9, bold=True))
            lbl.pack(fill="both", expand=True)

        content = tk.Frame(frame, bg=BG)
        content.pack(side="left", fill="x", expand=True, padx=(6, 0))

        head = tk.Frame(content, bg=BG)
        head.pack(fill="x")
        name_color = ACCENT if is_me else "#ffd700"
        tk.Label(head, text=nick, fg=name_color, bg=BG, font=_font(9, bold=True)).pack(side="left")
        tk.Label(head, text=t, fg="#555", bg=BG, font=_font(8)).pack(side="left", padx=(6, 0))

        if text.startswith("/me "):
            tk.Label(content, text=f"* {nick} {text[4:]}", fg="#aaa", bg=BG, font=_font(9, italic=True)).pack(anchor="w")
        else:
            tk.Label(content, text=text, fg=FG, bg=BG, font=_font(9), anchor="w", justify="left", wraplength=500).pack(anchor="w", fill="x")

        return frame

    def _refresh_msgs(self):
        for w in self.msg_inner.winfo_children():
            w.destroy()
        ch = self._current_channel()
        msgs = [m for m in self.client.all_msgs.values() if m["chan"] == ch]
        msgs.sort(key=lambda x: x["id"])
        for m in msgs[-200:]:
            self._create_msg_widget(m)
        self.msg_canvas.after(10, lambda: self.msg_canvas.yview_moveto(1.0))

    def _start_poll(self):
        def poll():
            while self.client.running:
                new = self.client.poll()
                if new:
                    self.root.after(0, lambda: self._on_new_msgs(new))
                time.sleep(2)
        threading.Thread(target=poll, daemon=True).start()

    def _on_new_msgs(self, msgs):
        ch = self._current_channel()
        for m in msgs:
            if m["chan"] == ch:
                self._create_msg_widget(m)
                self.msg_canvas.after(10, lambda: self.msg_canvas.yview_moveto(1.0))
            if m["user"] != self.client.nick:
                self.root.bell()
                self.root.after(0, lambda: self.root.attributes("-topmost", True))
                self.root.after(100, lambda: self.root.attributes("-topmost", False))

    def _quit(self):
        self.client.running = False; self.root.destroy()

def main():
    app = ChatGUI(); app.root.mainloop()

if __name__ == "__main__":
    main()
