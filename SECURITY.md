# セキュリティポリシー

## 脆弱性の報告

脆弱性や秘密情報の混入を見つけた場合は、公開Issueへトークン、Discord ID、
サーバー構成、ログ、`.env`の内容を貼らないでください。GitHubの
「Report a vulnerability」が表示される場合は、非公開のSecurity Advisoryから
報告してください。

Discord Botトークンが公開された可能性がある場合は、コード修正より先にDiscord
Developer Portalでトークンを再発行してください。Git履歴から削除しただけでは、
既に取得されたトークンを無効化できません。

## 対応範囲

原則として最新の`main`を対象に確認します。運用環境固有の`.env`、本番DB、ログ、
Discordサーバー設定は公開リポジトリへ含めません。
