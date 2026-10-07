# pr_migrate — GitHub / Backlog Git の PR 移行・同期ツール

既存の Pull Request を移行元リポジトリから取得し、移行先リポジトリに同じ PR を作成します。
2 回目以降の実行では、追加・編集されたコメントやタイトル/本文の変更、クローズ/マージ状態を差分同期します。

| 移行元 → 移行先 | PR 作成 | コメント | インラインコメント | 状態（クローズ） |
|---|---|---|---|---|
| GitHub → GitHub | ○ | ○ | ○（同じ行に再現） | ○ |
| GitHub → Backlog | ○ | ○ | △（ファイル名・行・差分付きの通常コメント） | ×（手動） |
| Backlog → GitHub | ○ | ○ | — | ○ |
| Backlog → Backlog | ○ | ○ | — | ×（手動） |

## 必要なもの

- Python 3.8 以上（追加パッケージ不要）
- git（ブランチ・コミットの転送に使用）
- GitHub トークン：Fine-grained token なら対象リポジトリの **Pull requests: Read and write** と **Contents: Read and write**
- Backlog API キー：個人設定 → API で発行
- Backlog Git への push 権限（HTTPS なら Git 資格情報マネージャー、または SSH 鍵）

## セットアップ

```bat
cd F:\work\pr_migrate
copy config.example.json config.json
copy .env.example .env
rem config.json と .env を編集
python pr_migrate.py check
```

`check` で API と git の両方に接続できることを確認します。

## 使い方

```bat
rem 移行元 PR の一覧と移行状況
python pr_migrate.py list

rem まずは dry-run で内容を確認（書き込み・push は行いません）
python pr_migrate.py migrate --dry-run

rem 1 件だけ試す
python pr_migrate.py migrate --pr 12

rem 全件移行（未移行は作成、移行済みは同期）
python pr_migrate.py migrate

rem 移行済み PR のコメント等だけ同期（新規作成しない）
python pr_migrate.py sync

rem 5 分ごとに同期し続ける
python pr_migrate.py sync --watch 300
```

主なオプション：

| オプション | 説明 |
|---|---|
| `-c FILE` | 設定ファイル（既定 `config.json`）。移行ペアごとに設定ファイルを分けられます |
| `--pr N [N ...]` | 対象 PR 番号を指定 |
| `--state open/closed/merged/all` | 対象の状態（既定 all） |
| `--since 2024-01-01` | この日以降に作成された PR のみ |
| `--limit N` | 最大件数 |
| `--dry-run` | 実行内容の表示のみ |
| `--no-push` | ブランチ転送をしない（移行先にブランチが既にある場合） |
| `--force-push` | head ブランチを強制更新（移行元で force-push された場合） |
| `--no-comments` | コメントを同期しない |
| `--debug` | 詳細ログ |

## 動作の仕組み

1. **ブランチ転送**：移行元を `.work/` にベアリポジトリとして取得し、PR の head コミットを移行先へ push します。
   コミット SHA がそのまま移るため、差分・コミット履歴は元と同一になります。
   GitHub の `refs/pull/*/head` も取得するので、削除済みブランチやフォークからの PR も再現できます。
2. **オープンな PR**：元と同じブランチ名（`feature/x → main`）で作成します。移行先に base ブランチが無ければ作成します。
3. **クローズ/マージ済み PR**（`closed_pr_strategy: snapshot`）：マージ後の base では差分が無くなるため、
   当時のベースコミットを `pr-migrate/pr-<番号>/base` として作成し、そこに向けた PR を作ります。
   移行先が GitHub ならその後クローズします。
4. **本文**：先頭に移行元 PR の URL・作成者・作成日時・状態を付けます（`add_header`）。
5. **コメント**：通常コメント・レビュー（承認/変更要求）・レビューコメントを作成日時順に投稿します。
   投稿者は API トークンのユーザーになるため、各コメントの先頭に元の投稿者と日時を記載します。
6. **状態ファイル**：`state/config.state.json` に「移行元 PR ↔ 移行先 PR」「元コメント ↔ 移行先コメント」の対応を保存し、
   再実行時は差分（新規コメント・編集されたコメント・本文変更・状態変化）だけ反映します。
   途中で失敗しても再実行すれば続きから処理されます。**このファイルは消さないでください。**

## 注意点・制限

- **通知**：移行先でコメントを大量に投稿するため、ウォッチしている人に通知が飛びます。
  移行用のボットアカウントのトークンを使う、事前に周知する、などをおすすめします。@メンションは付けません。
- **Backlog の制限**：Backlog API では PR のクローズ/マージ・インラインコメントの作成ができません。
  クローズ/マージ済み PR は Backlog 上で手動でクローズしてください（注記コメントは自動で付きます）。
- **添付ファイル・画像**：本文やコメント中の画像は元 URL のまま参照されます（移行元が非公開だと表示できない場合があります）。
- **記法**：本文は変換しません。Backlog 記法のプロジェクトとの間で移行する場合は `text_format` を合わせてください（ヘッダー部分のみ変換されます）。
- **レビュー承認**：承認・変更要求は「〇〇が承認しました」というコメントとして再現します（承認状態そのものは移せません）。
- **インラインコメント（GitHub→GitHub）**：元のコミットが移行先 PR に存在しない場合（force-push で消えた等）は、ファイル名・行番号付きの通常コメントになります。
- **リアクション・レビュー依頼者・ラベル・マイルストーン** は移行しません。
- `.env`・`config.json`・`state/`・`.work/` は `.gitignore` 済みです。トークンをコミットしないよう注意してください。
