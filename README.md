# discord-monitor

GitHub Actionsで稼働する無料の重要情報監視。PC常駐・有料APIは使用しません。
既存の取得処理と履歴を引き継ぎ、通知判定・配信履歴を共通化しています。

## 通知先

| チャンネル | 内容 | Secret |
|---|---|---|
| info_monitor_main | Windows 11、Discord、Steam、Medal.tv | DISCORD_WEBHOOK_IMPORTANT |
| game-monitor_dbd | DbDの重要情報と補完 | DISCORD_WEBHOOK_DBD |
| game-monitor_ow | OWの重要情報と補完 | DISCORD_WEBHOOK_OW |
| game-monitor_apex | Apexの重要情報と補完 | DISCORD_WEBHOOK_APEX |

各通知は `DISCORD_MENTION_USER_ID` のユーザーにメンションします。
ゲームの補完情報も該当ゲームのチャンネルだけに送信します。
チャンネル名の変更でWebhookの宛先IDは変わりません。

## 取得と判定

- 全体監視は6時間ごと、ゲーム別監視は3時間ごと。Actionsの混雑で開始が遅れる場合があります。
- WindowsはMicrosoft release healthの23H2/24H2/25H2/26H1、Windows公式ブログ、Windows Latest、BleepingComputerを利用。重大な障害を対象にし、通常の機能追加だけでは通知しません。
- Discord公式ブログ、Steamクライアント公式ニュース、Medal公式features/changelogの既存取得を維持。
- ゲームは日本語公式ニュース、Steam公式ニュース、OW公式パッチノート、BHVR公式パッチノート、既存の補助フィードを利用。
- 本文を含めて新キャラクター・新マップ・大型更新・重要な開発発表等を判定。軽微な修正や宣伝・大会情報は除外します。
- 取得できた日本語公式記事を優先。英語のみの場合は公開記事の要点を既存のMyMemory無料翻訳で日本語化します。翻訳失敗時は未送信キューに保持します。
- 直近7日を毎回再評価。導入時の手動実行だけ `recovery_days=14` を選択できます。期限前にキューに入った未送信記事は期限後も保持します。
- URL・公式記事ID・Steam ID・見出し・バージョン・パッチ日付・公式英語見出しとURL slug等で重複を照合します。未知の表現差まで完全に判別する意味理解モデルではありません。

## 配信履歴と障害時の動作

共通履歴は既存 `important-monitor-state` ブランチの `delivery-state-v2.json`。
全監視を同じconcurrency groupで直列化しています。

- `seen`: 取得確認。通知済みの根拠にはしません。
- `queue`: 重要と判定した未送信記事。1実行最大5件を送信し、残りは次回へ。
- `pending`: 投稿前に予約を永続化。送信結果が不明なら残し、そのチャンネルへ自動再送しません。
- `sent` / `receipts`: DiscordのメッセージIDが返った場合だけ確定。メンション確認も記録します。
- 明確なHTTP拒否（400/401/403/404/405/413/429）はキューを残して次回再試行。通信切断・5xx・ID欠落は不明扱い。
- 情報源の一時的通信失敗は最大3回再試行。取得失敗を0件成功として扱わず、Actions概要と履歴に記録。対象ジャンルの公式情報源がすべて失敗した場合は実行を失敗扱いにします。
- 旧ゲーム履歴はGitの時系列から「pending→新しいメッセージID」の遷移を監査して移行。初回baselineや後からseenをコピーしたsentは配信実績として扱いません。旧state・旧コミットは保持します。

`pending` が残った場合は、予約時刻・URLと実際のDiscordを照合してください。
投稿がある場合はそのIDで確定し、確実に未投稿と確認できた場合だけ予約を解除します。
確認せずにpending/sent/履歴ファイルを消すと重複や取りこぼしにつながります。

## 検証

```sh
python3 -m unittest -b -q test_monitor
python3 monitor.py important --check-sources
```

39件の回帰テストで重要度・seen/sent分離・日本語優先・重複排除・ジャンル別配信・失敗時保持を検証します。
各workflowの `test_notification=true` は接続テスト専用です。同じ検証IDは再投稿しません。
通常実行は `false` のままにします。実行概要でdelivered/queued/pendingとsource failuresを確認できます。

## 日本語公式Xの調査結果

X自動取得は未実装です。日本語公式サイトを代替として実装しましたが、Xだけの告知は取得できません。

- OWの `@jpPlayOverwatch` は[Blizzard公式ページ](https://overwatch.blizzard.com/ja-jp/news/22770110/)から確認。
- DbDの現在の日本語公式は `@DbDBHVR_JP`。[BHVRの公式アカウント一覧](https://support.deadbydaylight.com/hc/en-us/articles/4408560255764-What-are-your-official-social-media-channels)で確認。
- ApexはEA日本語ニュースを採用。日本語Xの候補EAJapanは今回の現行公式リンク確認が不十分なため、自動監視対象として確定していません。
- [X公式API](https://docs.x.com/x-api/getting-started/pricing)は従量課金のため不採用。
- 公開Web取得は403等で安定取得できず、ログイン済みPCブラウザーへの依存はPC OFF条件を満たしません。
- [Nitter](https://github.com/zedeus/nitter)等の非公式経路はセッショントークンや運営インスタンスに依存し、無料・安定・無人運用を確認できないため不採用。

Windows専用workflowの廃止後も旧チャンネルは過去メッセージ保管用として保持します。
Monitor Health Checkは定期実行の失敗・停止を既存の運用通知先に報告し、削除済みworkflowを監視対象から除外します。
