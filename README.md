# PortWho

どのポートを誰（どのプロセス）が使っているかを確認して kill する、単体 GUI ツール。

`node` / `python` / `dart` / `bun` などの開発サーバーがポートを掴んだまま残ったとき、
ポート番号やプロセス名で素早く特定して終了させるための小物。
**自分自身はポートを使わない**（`netstat` と CIM を叩いて表示するだけ）ので、
FlutterBoard などポートを使うツールと干渉しない。

## 機能

- リッスン中のポート一覧を表示（ポート / Proto / PID / 種別 / プロセス名 / 開く / コマンドライン）
- ポート番号・プロセス名・コマンドラインでインクリメンタルフィルタ（例: `3000`, `node`, `python`）
- **種別 / Web のセレクトボックス絞り込み**（種別=Node/Python/…、Web=「Web のみ/Web 以外」）。
  テキストフィルタと AND で併用、「クリア」で一括リセット
- **開発ランタイムを強調**: `node` / `bun` / `deno` / `python` / `dart` / `go` / `java` / `php` /
  `ruby` / `.NET` を色チップ＋太字で区別（特に開発用途と思われるものが一目で分かる）
- **Web サーバーにリンク**: HTTP を喋るポートに「開く ↗」を表示。クリック / ダブルクリックで
  `http://localhost:PORT` をブラウザで開く（軽量 HTTP プローブで実際に喋るものだけ判定）
- 行を選んで kill（プロセスツリーごと強制終了。誤操作防止のためボタン経由のみ）
- 列ヘッダクリックでソート、自動更新トグル

## 使い方

### ソースから実行（開発・お試し）

```
python portwho.py
```

依存は標準ライブラリのみ（`tkinter`）。追加インストール不要。
`python ports.py [フィルタ]` で CLI としても使える（例: `python ports.py node`）。

### exe をビルドして使う（配布・常用）

```
build.cmd
```

`dist\PortWho.exe` が生成される（PyInstaller の onefile / windowed）。
PyInstaller が未導入なら自動で `pip install` する。生成後はダブルクリックで起動。

## 仕組み

| OS | ポート→PID | PID→名前 | PID→コマンドライン |
|----|-----------|---------|------------------|
| Windows | `netstat -ano` | `tasklist` | PowerShell `Get-CimInstance Win32_Process` |
| Unix | `ss -tlnp` / `lsof` | （同上） | `ps -eo pid,args` |

- Windows 11 の新しいビルドでは `wmic` が既定で削除されているため、
  コマンドライン取得は `wmic` を使わず PowerShell の `Get-CimInstance` を使用。
- 日本語パスの文字化けを避けるため、PowerShell の出力は UTF-8 → Base64 で受け渡し、
  Python 側で確実に復号している。
- Web 判定は TCP リッスンポートへ `GET /` を短いタイムアウトで投げ、応答が `HTTP/` で
  始まるものだけを「Web サーバー」とみなす（localhost への軽量プローブ・並列実行）。

## ファイル

```
ports.py     — ポート列挙・kill（tkinter 非依存の純ロジック / CLI でも使える）
portwho.py   — tkinter GUI
build.cmd    — PyInstaller で単一 exe 化
```

`ports.py` は単体でも動く: `python ports.py node` で `node` を含む行だけ表示。
