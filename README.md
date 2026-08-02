# ランク人狼Bot

13人固定・ボタンUI・レーティング付きの Discord 人狼ゲーム Bot です。
役職の配布から昼の議論・投票、夜の役職行動、シーズン制のレート/ランクまでを
すべて自動で進行します。

- 現在のバージョン: **v0.32**
- 詳細仕様: **[SPEC.md](SPEC.md)**
- ライセンス: **[LICENSE](LICENSE)**（後述）

> このBotは **ソース公開・非商用** です（「オープンソース」ではありません）。
> 改変・配布・譲渡は自由ですが、営利利用と営利団体による利用は禁止です
> （配信・動画そのものから生じる収益のみ例外的に許可）。詳しくは LICENSE を
> 必ずお読みください。**土台として自由に使い、独自のルールやレギュレーションに
> 改変してもらうことを歓迎します。**

## 主な機能

- 13人固定（人狼3 / 狂人1 / 占い師1 / 霊媒師1 / 狩人1 / 村人6）
- すべてボタン/セレクトUIで操作（スラッシュコマンドは管理用のみ）
- 夜の役職行動はDM。襲撃は夜の間は変更可、占い・護衛は実行確認後に確定し変更不可
- 占い結果は確定と同時にDMへ届く（エフェメラル表示とは別に通常DMでも残る）
- ゲーム開始直後は時間切れでは進まず、**参加者全員が「役職を確認した」を押すと議論開始**
- 夜は時間切れでは明けず、**DMに届くパネルで生存者全員が「朝を迎える」を押すと朝**になる（GMのみGMメニューの「朝」で強制可）
- 人狼どうしのやり取りは**夜の制限時間で終了**（会話の中継も襲撃先の変更通知も止まる。襲撃先の選択自体は夜明けまで可能だが他の狼には伝わらない）
- 投票と破壊的なGM操作は確認してから確定（誤操作防止）
- ゲーム中のGM状況・操作は `#昼` の末尾、受付操作は `#参加受付` の末尾に維持
- 初心者・中級者・上級者・村長ロール説明は、全ロールから隠してAdministrator権限保持者だけに表示
- `#統計` は既存の `#総合` の直下に1つだけ配置
- ローカル固定卓は`.env`だけで追加でき、指定ロールまたはサーバー管理者だけに表示
- 起動時には既存チャンネル・カテゴリを並び替えず、保存済みIDと卓名から同じ構成を再利用
- `#統計` から不具合・分かりにくさ・改善要望を報告し、ゲーム状況とともにSQLiteへ保存
- `#募集` で7日先までの13人募集を作り、参加・補欠・GMを再起動後も保持して既存ロビーへ一括移行
- 卓の90分予約、補欠の自動繰り上げ、15分前DM、開催時のランク・GM条件再確認に対応
- `#統計` の本人専用UIで同村拒否を管理し、`#運営` で被拒否数・受付中GM・募集を管理
- `#統計` の「全体データ」で卓別の勝敗・日数・平和・処刑・初夜噛み・生存狼推移・時間帯・GMを表示。プレイヤー指標は試合時の相対ランク別に切替
- 占い・護衛・死亡日/死因・試合時ランク・GMを導入後の各試合へ保存し、個人統計にシーズン役職勝率・連勝/連敗・初夜襲撃・推薦数を追加
- サーバーミュートによる自動発言制御（昼/夜/投票/弁明/遺言）
- 固定プール制レート + エメラルドを含む相対ランク9段階 + シーズン制
- 終了後、霊媒師・初日処刑者・初夜襲撃死者が匿名で参加者へレート+1を贈る推薦
- 終了時に**全役職の行動・投票・死亡を発生順にまとめた進行ログ**を `#昼` へ掲示（拒否された操作も残るため二重実行の有無をその場で確認できる）
- Bot再起動をまたいでゲームを途中再開（状態をSQLiteへ常時保存）
- シーン切替の効果音（任意・後述の音声ライブラリが必要）

## 動作要件

- Python 3.14（3.14.6で検証）
- Discord Bot トークン（[Discord Developer Portal](https://discord.com/developers/applications) で作成）
- Bot に付与する権限: チャンネル管理 / ロール管理 / ニックネーム管理 /
  メッセージ管理 / DM送信 / **メンバーをミュート**
- **特権インテント**: Server Members Intent と Message Content Intent を
  Developer Portal で有効化（役職DMの中継・メンバー管理に必要）
- サーバー側で、**Bot のロールをランクロールより上位**に置くこと

## セットアップ

```sh
# 1. Python 3.14 を確認し、.venv と依存を準備
./scripts/setup_venv.sh

# 2. 設定ファイルを作成（対象サーバーIDも固定することを推奨）
cp .env.example .env
# .env の DISCORD_TOKEN / DISCORD_GUILD_ID を編集
chmod 600 .env

# 3. 起動（多重起動防止と準備完了確認つき）
.venv/bin/python scripts/start_bot_detached.py

# 停止
./scripts/stop_bot.sh
```

個別サーバー専用の固定卓が必要な場合だけ、`.env.example`の
`WEREWOLF_LOCAL_ROOMS_JSON`を参考に`.env`へ追加してください。公開用の標準構成は
初心者・中級者・上級者・総合の4卓です。ローカル卓の`room_id`と`name`は、導入後に
変更するとDBの復元対象やDiscordカテゴリが別卓扱いになるため変更しないでください。
設定値が壊れている場合は、一部だけ適用せずDiscord接続前に起動を中止します。

直接依存は `requirements.txt`、検証済みの推移依存を含む完全固定版は
`requirements-lock.txt` に記録しています。

Apple Siliconでは `/opt/homebrew/opt/python@3.14` をPATH探索より優先し、
Rosetta上のx86_64 Pythonは拒否します。稼働開始後にvenvを作り直す場合は、
必ず `./scripts/stop_bot.sh` でBotを停止してから `setup_venv.sh` を実行してください。
Bot稼働中または稼働状態を安全に確認できない場合、セットアップはvenvへ触れず中止します。

ログは `logs/bot.log`（5MB×3世代でローテーション）に出力されます。
起動時のスキーマ更新より前に`pre_migration`バックアップを作成し、作成できなければ
DBを変更せず起動を中止します。通常の起動時バックアップも別に保持します。
`DISCORD_GUILD_ID` を設定した場合、Botはその1サーバーだけを管理し、別サーバーへ
追加されてもゲーム状態を作成しません。直接 `bot.py` を起動した場合もランチャーと
同じプロセスロックを使うため、二重起動は拒否されます。

同梱AppleScriptは現在の実運用を誤って切り替えないよう、デスクトップ上の旧配置名
`bot`を先に、無い場合だけ`rank-werewolf-bot`を探します。`WEREWOLF_BOT_DIR`は
AppleScriptの実行環境へ明示的に渡した場合だけ最優先され、プロジェクトの`.env`からは
読まれません。Finderから実行する`.app`で任意配置を使う場合は、AppleScript内の探索先を
その絶対パスへ変更して再コンパイルしてください。

新規導入先に同名のDiscordカテゴリが既にある場合は、無関係なカテゴリを変更しないため
起動を中止します。そのカテゴリをこのBotの管理下へ引き継ぐと確認できた場合だけ、初回に
`.env`へ`ADOPT_EXISTING_LAYOUT=1`を設定してください。保存済みチャンネルIDがある既存運用は
この設定なしで従来の配置を再利用し、並び替えません。

> **効果音（任意）**: シーン切替のSEを鳴らすには `davey`、`PyNaCl`、`libopus` が
> 必要です。**discord.py 2.7系ではVoiceClientの接続にDAVE E2EE実装の `davey` が必須**で、
> 欠けると `davey library needed in order to use voice` になります。本プロジェクトは
> `discord.py[voice]==2.7.1`、`davey==0.1.6`、`PyNaCl==1.5.0` を固定しています。
> `setup_venv.sh` とBot起動時に、DAVEセッション生成・discord.py側の認識・libopusロードまで
> 検査します。`SE_ENABLED = True` のまま依存が不完全なら、無音で運用を続けず起動を停止します。
> macOS は `brew install opus`、Debian/Ubuntu は `apt install libopus0` を使用してください。
> Pythonとネイティブwheel/libopusのCPUアーキテクチャも一致している必要があります。
> libopusはプロジェクト同梱版をOSの自動探索より先にロードします。
> SEを使わない場合だけ `config.py` の `SE_ENABLED = False` へ変更してください。

音声依存だけを再確認する場合:

```sh
.venv/bin/python -m discord --version
.venv/bin/python -c 'import sounds; sounds.require_voice_ready()'
```

実際のDiscord VCへ接続して朝SEを1回鳴らす場合（BotがVCへ接続・再生・切断します）:

```sh
.venv/bin/python scripts/check_se_playback.py --scene morning
# 再生先を固定する場合
.venv/bin/python scripts/check_se_playback.py --scene morning --channel-id 123456789012345678
```

## カスタマイズ（土台として使う）

ルールやレギュレーションは主に **[config.py](config.py)** で変更できます。

| 変更したいもの | 定数 |
|---|---|
| 役職構成・人数 | `ROLE_DISTRIBUTION` / `MAX_PLAYERS` |
| 議論・夜・投票などの時間 | `DAY_DISCUSSION_*` / `NIGHT_*` / `VOTE_TIMEOUT` / `LAST_WILL_TIME` ほか |
| フェーズ間の猶予・ミュート整列 | `DISCUSSION_GRACE_TIME` / `MUTE_GRACE_TIME` / `MUTE_RETRY_DELAY` |
| レート計算 | `INITIAL_RATING` / `WIN_PARTICIPATION_BONUS` / `*_WIN_FIXED_POOL` / `RATING_FLOOR` |
| 終了後推薦の受付時間 | `POSTGAME_RECOMMENDATION_TIMEOUT` |
| ランク段階・相対評価の割合 | `RANK_SPECS` / `SEASON_RANK_PERCENTAGES` / `SEASON_RANK_MIN_GAMES` |
| 統計の率・平均を表示する最低サンプル数 | `STATS_MIN_SAMPLES` |
| 募集の占有時間・通知・上限・ランク選択肢 | `RECRUITMENT_*` / `PLAYER_BLOCK_LIMIT` |
| 標準卓の定義・参加条件 | `ROOM_DEFINITIONS` |
| 個別サーバー専用の固定卓 | `.env`の`WEREWOLF_LOCAL_ROOMS_JSON` |
| 当面管理者だけに見せる卓・村長説明 | `ADMIN_ONLY_ROOM_IDS` / `MAYOR_INFO_ADMIN_ONLY` |
| チャンネル/カテゴリ名 | `CH_*` / `VC_GAME` / `*_CATEGORY_NAME` |

進行ロジックそのものを変えたい場合は `room_runner.py`（1卓の進行）と
`views.py`（UI）が中心です。

## テスト

コードを変えたら、実際にDiscordへ繋がなくても挙動を検証できます。

```sh
# 単発ゲームを多数シミュレート（同じ seed なら結果は決定的）
.venv/bin/python simulate_games.py --runs 30

# レート分布の長期シミュレーション
.venv/bin/python simulate_games.py --mode population

# ユニットテスト
.venv/bin/python -m unittest discover -s tests

# コンパイル・依存整合・全ユニットテスト・ゲームシミュレーション
./scripts/run_checks.sh
```

## 構成

| ファイル | 役割 |
|---|---|
| `bot.py` | エントリーポイント |
| `game.py` | GameCog（全卓管理・イベントdispatch・スラッシュコマンド） |
| `room_runner.py` | 1卓のゲーム進行ロジック |
| `permissions.py` | チャンネル権限管理 |
| `views.py` | ボタン/セレクトUI・ヘルプ・統計・フィードバック受付 |
| `recruitment.py` | 募集・卓移行・通知・同村拒否・運営UI |
| `database.py` | SQLite（ゲーム状態・統計・募集・フィードバック。`data/` 配下） |
| `rating.py` | レート計算・相対ランク |
| `config.py` | 定数・標準卓定義（**カスタマイズの入口**） |
| `room_config.py` | `.env`のローカル固定卓を厳格に検証して読み込む |
| `sounds.py` | シーン切替SEの生成・再生 |
| `simulate_games.py` | オフライン・シミュレータ |

## ライセンス

**人狼Bot 非商用ライセンス v1.0** — 詳細は [LICENSE](LICENSE) を参照。

- 改変・配布・譲渡は自由（ただし同一ライセンスで、原著作者クレジットを残すこと）
- 営利活動は禁止（配信・動画そのものから生じる収益のみ例外的に許可）
- 営利団体による利用は目的を問わず禁止
- 無保証（AS IS）。利用は自己責任です

Copyright (c) 2026 ねいとくん。（連絡先: @kyousokundayo）

なお、Botである以上、本ライセンスとは別に Discord の利用規約・デベロッパー規約の
遵守が必要です。同梱・依存する第三者ライブラリ（discord.py, PyNaCl, libopus 等）には
それぞれのライセンスが適用されます。
