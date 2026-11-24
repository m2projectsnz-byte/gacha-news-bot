# ガチャ新商品 Bluesky 自動投稿ボット

日本のガチャガチャ(ガシャポン/カプセルトイ)の新商品を、英語+日本語の併記でBlueskyに自動投稿します。

## しくみ

1. **収集(週1回)**:Claude APIのWeb検索で、バンダイ(ガシャポン)・タカラトミーアーツ・ケンエレファント・海洋堂の新商品を検索
2. **検証**:公式ドメインのURLか、ページが実在するか、商品名がページ内にあるか、発売月が今日以降かを機械チェック(通らないものは捨てる)
3. **投稿文の生成**:価格・種類数・発売月はコードで整形(AIの言い換えによる数字ミスを防止)
4. **投稿(毎日)**:週の残り日数に合わせて1日1〜2件ずつ投稿。公式ページのリンクカード(OGP画像付き)で表示
5. 重複投稿の防止、投稿履歴は `state/` に保存され、GitHubに自動コミットされます

投稿イメージ:

```
New Gashapon from Bandai: "My Hero Academia Gyu-cotto" 🧸
Tiny figures hugging mini plushies. 5 designs, ¥500/spin, December 2026 in Japan.

バンダイ新作ガシャポン「僕のヒーローアカデミア ぎゅーこっと」
ぬいぐるみを抱くデフォルメフィギュア。全5種、1回500円、2026年12月発売予定。
#Gashapon #CapsuleToy #ガシャポン
```

## セットアップ手順

### 1. GitHubリポジトリを作る
- 新しいリポジトリを作成(非公開でOK)
- このフォルダの `gacha_bot.py` と `requirements.txt` をリポジトリ直下に置く
- `gacha-weekly.yml` は **`.github/workflows/gacha-weekly.yml`** として置く

### 2. Blueskyのアプリパスワードを作る
- Bluesky → 設定 → プライバシーとセキュリティ → アプリパスワード → 追加
- 通常のログインパスワードは使わないでください
- プロフィール文に「自動投稿のボットアカウントです」と明記しておくことをおすすめします

### 3. Anthropic APIキーを用意する
- Claude Console でAPIキーを発行
- 組織の管理者がConsoleでWeb検索を有効にしている必要があります

### 4. GitHubにSecretsを登録する
リポジトリの Settings → Secrets and variables → Actions → Secrets に以下を追加:

| 名前 | 値 |
|---|---|
| `ANTHROPIC_API_KEY` | AnthropicのAPIキー |
| `BSKY_HANDLE` | 例: `yourname.bsky.social` |
| `BSKY_APP_PASSWORD` | 手順2のアプリパスワード |

### 5. まず手動でドライラン
- Actions タブ → 「Gacha news bot」→ Run workflow → `dry_run` を ON のまま実行
- ログに、収集された商品と「投稿される予定の文章」が出ます(Blueskyには投稿されません)
- `state/queue.json` に候補が保存されます

### 6. 最初の数週間は承認モードで運用(初期設定)
- 初期設定では `REQUIRE_APPROVAL=true`:`state/queue.json` で `"approved": true` にした商品だけが投稿されます
- GitHub上で `state/queue.json` を開き、内容(`text` の文面も編集できます)を確認して `"approved": true` に変更して保存
- 事実(価格・発売月・商品名)を公式ページと見比べてください

### 7. 完全自動へ切り替える
- Settings → Secrets and variables → Actions → **Variables** に `REQUIRE_APPROVAL` = `false` を追加
- 以降は毎日03:00 UTC(日本時間12:00)に自動実行されます

## 設定(任意)

| 変数 | 既定値 | 内容 |
|---|---|---|
| `REQUIRE_APPROVAL` | `true` | 承認した商品のみ投稿 |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | Web検索に対応したモデル名(400エラーが出たら変更) |
| `MAX_ITEMS_PER_WEEK` | `10` | 週あたりの最大件数 |
| `STRICT_NAME_CHECK` | `true` | 商品名が公式ページに無ければ除外 |
| `OFFICIAL_DOMAINS` | 主要メーカー | リンク先として許可するドメイン(カンマ区切り) |

## 運用上の注意

- 発売日・価格は「開発中の情報」として変更される場合があります。承認モードで公式ページを確認してから投稿するのが安全です
- 投稿する画像は、公式ページのOGP画像をBlueskyのリンクカードとして表示する形です(画像ファイルの転載は行いません)
- 週の件数が5件に満たない場合は、ログに警告が出ます(新商品の発表が少ない週など)
- GitHub Actionsが失敗すると、GitHubから通知メールが届きます

## 手動実行(ローカル)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
DRY_RUN=true python gacha_bot.py collect
DRY_RUN=true python gacha_bot.py post
```
