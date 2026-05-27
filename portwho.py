"""PortWho — どのポートを誰が使っているかを確認して kill する GUI ツール

- リッスン中のポート一覧を表示（ポート / Proto / PID / 種別 / プロセス名 / 開く / コマンドライン）
- ポート番号・プロセス名・コマンドラインでインクリメンタルフィルタ（例: 3000, node, python）
- 開発ランタイム（node/bun/deno/python/dart/go/java/php/ruby/.NET）を色チップ＋太字で強調
- HTTP を喋るポートには「開く ↗」リンクを付け、クリックで http://localhost:PORT を開く
- 行を選んで kill（プロセスツリーごと強制終了）
- 自分自身はポートを使わない（netstat/CIM を叩いて表示するだけ）

実行: python portwho.py
ビルド: build.cmd → dist/PortWho.exe
"""
from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

import ports

# 種別キー → (表示ラベル, 色チップの色)
KIND_META = {
    "node":   ("Node", "#43a047"),
    "bun":    ("Bun", "#e08c69"),
    "deno":   ("Deno", "#6c63ff"),
    "python": ("Python", "#3776ab"),
    "dart":   ("Dart", "#13b9c2"),
    "go":     ("Go", "#00add8"),
    "java":   ("Java", "#e76f00"),
    "php":    ("PHP", "#777bb3"),
    "ruby":   ("Ruby", "#cc342d"),
    "dotnet": (".NET", "#7a4ad6"),
    "other":  ("", "#c0c0c0"),
}

# 種別の表示名（other は「その他」）と、セレクトボックス用の逆引き
KIND_DISPLAY = {k: (lbl or "その他") for k, (lbl, _c) in KIND_META.items()}
DISPLAY_TO_KIND = {v: k for k, v in KIND_DISPLAY.items()}

# 列定義: (key, 見出し, 幅, anchor)
COLS = [
    ("port", "ポート", 64, tk.E),
    ("proto", "Proto", 52, tk.CENTER),
    ("pid", "PID", 64, tk.E),
    ("kind", "種別", 64, tk.CENTER),
    ("name", "プロセス", 150, tk.W),
    ("web", "開く", 86, tk.CENTER),
    ("cmdline", "コマンドライン", 520, tk.W),
]
COL_KEYS = [c[0] for c in COLS]
WEB_COL_ID = f"#{COL_KEYS.index('web') + 1}"  # identify_column 用（#0 はツリー列）
AUTO_INTERVAL_MS = 4000


class PortWhoApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PortWho — ポート使用状況")
        self.geometry("1100x560")
        self.minsize(680, 320)

        self._all: list[dict] = []        # 直近に取得した全行（フィルタ前）
        self._row_by_iid: dict[str, dict] = {}
        self._sort_key = "port"
        self._sort_desc = False
        self._loading = False

        self.filter_var = tk.StringVar()
        self.status_var = tk.StringVar(value="準備完了")
        self.auto_var = tk.BooleanVar(value=False)
        self.kind_var = tk.StringVar(value="すべて")
        self.web_var = tk.StringVar(value="すべて")

        self._chips = self._make_chips()
        self._build_ui()
        self.refresh()

    # 種別ごとの色チップ（実行時生成・外部アセット不要 → PyInstaller でも安全）
    def _make_chips(self) -> dict[str, tk.PhotoImage]:
        chips = {}
        for kind, (_label, color) in KIND_META.items():
            img = tk.PhotoImage(width=12, height=12)
            img.put(color, to=(0, 0, 12, 12))
            chips[kind] = img
        return chips

    # ── レイアウト ───────────────────────────────────────────────────────────
    # グローバル規約: フッターは side=BOTTOM で先に pack してから本体を pack する
    def _build_ui(self):
        # 1. 下部ボタンバー（最初に BOTTOM）
        bar = tk.Frame(self)
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=6)
        self.kill_btn = tk.Button(bar, text="選択を kill", state=tk.DISABLED,
                                  command=self.kill_selected, width=14)
        self.kill_btn.pack(side=tk.LEFT)
        self.open_btn = tk.Button(bar, text="ブラウザで開く", state=tk.DISABLED,
                                  command=self.open_selected, width=14)
        self.open_btn.pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(bar, text="更新", command=self.refresh, width=8).pack(side=tk.LEFT, padx=(8, 0))
        tk.Checkbutton(bar, text=f"自動更新 ({AUTO_INTERVAL_MS // 1000}s)",
                       variable=self.auto_var, command=self._toggle_auto).pack(side=tk.LEFT, padx=(12, 0))
        tk.Button(bar, text="閉じる", command=self.destroy, width=8).pack(side=tk.RIGHT)

        # 2. ステータス（次に BOTTOM）
        status_bar = tk.Frame(self)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 2))
        tk.Label(status_bar, textvariable=self.status_var, anchor=tk.W,
                 fg="#555").pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 3. 上部フィルタバー（TOP）: テキスト + 種別/Web のセレクトボックス
        top = tk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(8, 4))
        tk.Label(top, text="フィルタ:").pack(side=tk.LEFT)

        # 右寄せグループ（side=RIGHT は pack 順が右→左になる点に注意）
        self.web_combo = ttk.Combobox(top, state="readonly", width=9, textvariable=self.web_var,
                                      values=["すべて", "Web のみ", "Web 以外"])
        self.web_combo.pack(side=tk.RIGHT, padx=(2, 0))
        tk.Label(top, text="Web:").pack(side=tk.RIGHT, padx=(10, 2))
        self.kind_combo = ttk.Combobox(top, state="readonly", width=9, textvariable=self.kind_var,
                                       values=["すべて"])
        self.kind_combo.pack(side=tk.RIGHT, padx=(2, 0))
        tk.Label(top, text="種別:").pack(side=tk.RIGHT, padx=(10, 2))
        tk.Button(top, text="クリア", width=5, command=self._clear_filters).pack(side=tk.RIGHT, padx=(10, 0))
        self.web_combo.bind("<<ComboboxSelected>>", lambda _e: self._render())
        self.kind_combo.bind("<<ComboboxSelected>>", lambda _e: self._render())

        ent = tk.Entry(top, textvariable=self.filter_var)
        ent.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 10))
        ent.bind("<KeyRelease>", lambda _e: self._render())

        # 4. 本体 Treeview（最後に TOP, expand）。#0 ツリー列を色チップ表示に使う
        body = tk.Frame(self)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=2)
        self.tree = ttk.Treeview(body, columns=COL_KEYS, show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=30, minwidth=30, stretch=False, anchor=tk.CENTER)
        for key, label, width, anchor in COLS:
            self.tree.heading(key, text=label, command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "cmdline"))
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # 開発ランタイムの行を太字に
        self.tree.tag_configure("dev", font=("TkDefaultFont", 9, "bold"))

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-1>", self._on_click, add="+")
        self.tree.bind("<Motion>", self._on_motion, add="+")
        self.tree.bind("<Double-1>", self._on_double)

    # ── データ取得（別スレッド: PowerShell が ~1s 掛かるため UI を固めない） ──────
    def refresh(self):
        if self._loading:
            return
        self._loading = True
        self.status_var.set("更新中…")
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            rows = ports.list_listening()
            err = None
        except Exception as e:  # noqa: BLE001
            rows, err = [], str(e)
        self.after(0, lambda: self._apply(rows, err))

    def _apply(self, rows, err):
        self._loading = False
        self._all = rows
        if err:
            self.status_var.set(f"取得エラー: {err}")
        self._refresh_kind_choices()
        self._render()
        # Web 判定はバックグラウンドで後追い（一覧表示は待たせない）
        tcp = [r["port"] for r in rows if r["proto"] == "TCP"]
        if tcp:
            threading.Thread(target=self._probe, args=(tcp,), daemon=True).start()
        if self.auto_var.get():
            self.after(AUTO_INTERVAL_MS, self.refresh)

    def _probe(self, tcp_ports):
        web = ports.probe_web_ports(tcp_ports)
        self.after(0, lambda: self._apply_web(web))

    def _apply_web(self, web: set[int]):
        for r in self._all:
            r["web"] = (r["proto"] == "TCP" and r["port"] in web)
            r["url"] = f"http://localhost:{r['port']}" if r["web"] else ""
        self._render()
        self._on_select(None)  # 選択中なら「開く」ボタンの活性を更新

    # ── 描画・フィルタ・ソート ────────────────────────────────────────────────
    def _filtered(self) -> list[dict]:
        q = self.filter_var.get().strip().lower()
        rows = self._all
        if q:
            rows = [r for r in rows if q in str(r["port"]) or q in r["name"].lower()
                    or q in r["cmdline"].lower() or q in r.get("kind", "").lower()]

        kindsel = self.kind_var.get()
        if kindsel and kindsel != "すべて":
            k = DISPLAY_TO_KIND.get(kindsel)
            rows = [r for r in rows if r.get("kind") == k]

        websel = self.web_var.get()
        if websel == "Web のみ":
            rows = [r for r in rows if r.get("web")]
        elif websel == "Web 以外":
            rows = [r for r in rows if not r.get("web")]

        def sort_key(r):
            if self._sort_key in ("port", "pid"):
                return int(r.get(self._sort_key, 0))
            if self._sort_key == "web":
                return 1 if r.get("web") else 0
            return str(r.get(self._sort_key, "")).lower()

        return sorted(rows, key=sort_key, reverse=self._sort_desc)

    def _render(self):
        sel_pid = self._selected_pid()
        self.tree.delete(*self.tree.get_children())
        self._row_by_iid = {}
        rows = self._filtered()
        for r in rows:
            kind = r.get("kind", "other")
            label = KIND_META.get(kind, ("", ""))[0]
            weblabel = "開く ↗" if r.get("web") else ""
            tags = ("dev",) if kind in ports.DEV_KINDS else ()
            iid = self.tree.insert(
                "", tk.END, text="", image=self._chips.get(kind, self._chips["other"]),
                values=(r["port"], r["proto"], r["pid"], label, r["name"], weblabel, r["cmdline"]),
                tags=tags,
            )
            self._row_by_iid[iid] = r
            if r["pid"] == sel_pid:
                self.tree.selection_set(iid)
        if not self._loading:
            total, shown = len(self._all), len(rows)
            suffix = "" if total == shown else f"（全 {total} 中 {shown} 件表示）"
            self.status_var.set(f"{shown} 件{suffix}")

    def _sort_by(self, key):
        if self._sort_key == key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key, self._sort_desc = key, False
        self._render()

    def _refresh_kind_choices(self):
        """現在の取得結果に存在する種別だけをセレクトボックスに反映。"""
        present = [KIND_DISPLAY[k] for k in KIND_META
                   if any(r.get("kind") == k for r in self._all)]
        self.kind_combo["values"] = ["すべて"] + present
        if self.kind_var.get() not in self.kind_combo["values"]:
            self.kind_var.set("すべて")

    def _clear_filters(self):
        self.filter_var.set("")
        self.kind_var.set("すべて")
        self.web_var.set("すべて")
        self._render()

    # ── 選択・クリック・kill・open ───────────────────────────────────────────
    def _row_at(self, iid) -> dict | None:
        return self._row_by_iid.get(iid)

    def _selected_row(self) -> dict | None:
        sel = self.tree.selection()
        return self._row_by_iid.get(sel[0]) if sel else None

    def _selected_pid(self) -> int | None:
        r = self._selected_row()
        return r["pid"] if r else None

    def _on_select(self, _e):
        r = self._selected_row()
        self.kill_btn.config(state=tk.NORMAL if r else tk.DISABLED)
        self.open_btn.config(state=tk.NORMAL if (r and r.get("web")) else tk.DISABLED)
        if r:
            tail = f"  →  {r['url']}" if r.get("web") else ""
            self.status_var.set(f"PID {r['pid']}  {r['name']}  {r['cmdline']}{tail}")

    def _on_click(self, event):
        # 「開く」列のリンクをクリック → ブラウザで開く（行選択は既定動作のまま）
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != WEB_COL_ID:
            return
        r = self._row_at(self.tree.identify_row(event.y))
        if r and r.get("web"):
            webbrowser.open(r["url"])

    def _on_motion(self, event):
        cursor = ""
        if self.tree.identify_region(event.x, event.y) == "cell" \
                and self.tree.identify_column(event.x) == WEB_COL_ID:
            r = self._row_at(self.tree.identify_row(event.y))
            if r and r.get("web"):
                cursor = "hand2"
        if self.tree.cget("cursor") != cursor:
            self.tree.configure(cursor=cursor)

    def _on_double(self, _e):
        # ダブルクリックは Web 行ならブラウザを開く（kill は誤操作防止のためボタン経由のみ）
        r = self._selected_row()
        if r and r.get("web"):
            webbrowser.open(r["url"])

    def open_selected(self):
        r = self._selected_row()
        if r and r.get("web"):
            webbrowser.open(r["url"])

    def kill_selected(self):
        r = self._selected_row()
        if not r:
            return
        if not messagebox.askyesno(
            "kill の確認",
            f"次のプロセスを強制終了します。よろしいですか?\n\n"
            f"ポート: {r['port']} ({r['proto']})\n"
            f"PID: {r['pid']}\n"
            f"プロセス: {r['name']}\n"
            f"コマンドライン:\n{r['cmdline'] or '(取得できず)'}",
            icon="warning",
        ):
            return
        ok, msg = ports.kill_pid(r["pid"])
        if ok:
            self.status_var.set(f"PID {r['pid']} ({r['name']}) を終了しました")
        else:
            messagebox.showerror("kill 失敗", msg or f"PID {r['pid']} の終了に失敗しました")
            self.status_var.set(f"kill 失敗: {msg}")
        self.after(500, self.refresh)

    def _toggle_auto(self):
        if self.auto_var.get():
            self.refresh()


def main():
    PortWhoApp().mainloop()


if __name__ == "__main__":
    main()
